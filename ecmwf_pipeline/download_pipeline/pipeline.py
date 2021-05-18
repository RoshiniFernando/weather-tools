"""Primary ECMWF Downloader Workflow."""

import argparse
import copy as cp
import functools
import itertools
import logging
import os
import tempfile
import typing as t

import apache_beam as beam
from apache_beam.io.gcp import gcsio
from apache_beam.options.pipeline_options import PipelineOptions, SetupOptions

from .clients import CLIENTS, Client, FakeClient
from .manifest import Manifest, Location, MockManifest
from .parsers import process_config, parse_manifest_location
from .stores import Store, TempFileStore


def configure_logger(verbosity: int) -> None:
    """Configures logging from verbosity. Default verbosity will show errors."""
    logging.basicConfig(level=(40 - verbosity * 10))


def prepare_target_name(config: t.Dict) -> str:
    """Returns name of target location."""
    partition_keys = config['parameters']['partition_keys']
    partition_key_values = [config['selection'][key][0] for key in partition_keys]
    target = config['parameters']['target_template'].format(*partition_key_values)

    return target


def skip_partition(config: t.Dict, store: Store = gcsio.GcsIO()) -> bool:
    """Return true if partition should be skipped."""

    if 'force_download' not in config['parameters'].keys():
        return False

    if config['parameters']['force_download']:
        return False

    target = prepare_target_name(config)
    if store.exists(target):
        logging.info(f'file {target} found, skipping.')
        return True

    return False


def prepare_partition(config: t.Dict, *, manifest: Manifest, store: Store = gcsio.GcsIO()) -> t.Iterator[t.Dict]:
    """Iterate over client parameters, partitioning over `partition_keys`."""
    partition_keys = config['parameters']['partition_keys']
    selection = config.get('selection', {})

    # Produce a Cartesian-Cross over the range of keys.
    # For example, if the keys were 'year' and 'month', it would produce
    # an iterable like: ( ('2020', '01'), ('2020', '02'), ('2020', '03'), ...)
    fan_out = itertools.product(*[selection[key] for key in partition_keys])

    # If the `parameters` section contains subsections (e.g. '[parameters.1]',
    # '[parameters.2]'), collect a repeating cycle of the subsection key-value
    # pairs. Otherwise, store empty dictionaries.
    #
    # This is useful for specifying multiple API keys for your configuration.
    # For example:
    # ```
    #   [parameters.deepmind]
    #   api_key=KKKKK1
    #   api_url=UUUUU1
    #   [parameters.research]
    #   api_key=KKKKK2
    #   api_url=UUUUU2
    #   [parameters.cloud]
    #   api_key=KKKKK3
    #   api_url=UUUUU3
    # ```
    extra_params = [params for _, params in config['parameters'].items() if isinstance(params, dict)]
    params_loop = itertools.cycle(extra_params) if extra_params else itertools.repeat({})

    # Output a config dictionary, overriding the range of values for
    # each key with the partition instance in 'selection'.
    # Continuing the example, the selection section would be:
    #   { 'foo': ..., 'year': ['2020'], 'month': ['01'], ... }
    #   { 'foo': ..., 'year': ['2020'], 'month': ['02'], ... }
    #   { 'foo': ..., 'year': ['2020'], 'month': ['03'], ... }
    #
    # For each of these 'selection' sections, the output dictionary will
    # overwrite parameters from the extra param subsections (above),
    # evenly cycling through each subsection.
    # For example:
    #   { 'parameters': {... 'api_key': KKKKK1, ... }, ... }
    #   { 'parameters': {... 'api_key': KKKKK2, ... }, ... }
    #   { 'parameters': {... 'api_key': KKKKK3, ... }, ... }
    #   { 'parameters': {... 'api_key': KKKKK1, ... }, ... }
    #   { 'parameters': {... 'api_key': KKKKK2, ... }, ... }
    #   { 'parameters': {... 'api_key': KKKKK3, ... }, ... }
    #   ...
    for option, params in zip(fan_out, params_loop):
        copy = cp.deepcopy(selection)
        out = cp.deepcopy(config)
        for idx, key in enumerate(partition_keys):
            copy[key] = [option[idx]]
        out['selection'] = copy
        out['parameters'].update(params)
        if skip_partition(out, store):
            continue

        selection = copy
        location = prepare_target_name(out)
        user = out['parameters'].get('user_id', 'unknown')
        manifest.schedule(selection, location, user)

        yield out


def fetch_data(config: t.Dict,
               *,
               client: Client,
               manifest: Manifest = MockManifest(Location('location-for-test')),
               store: Store = gcsio.GcsIO()) -> None:
    """
    Download data from a client to a temp file, then upload to Google Cloud Storage.
    """
    dataset = config['parameters'].get('dataset', '')
    target = prepare_target_name(config)
    selection = config['selection']
    user = config['parameters'].get('user_id', 'unknown')

    with manifest.transact(selection, target, user):
        with tempfile.NamedTemporaryFile() as temp:
            logging.info(f'Fetching data for {target}')
            client.retrieve(dataset, selection, temp.name, log_prepend=target)

            # upload blob to gcs
            logging.info(f'Uploading to GCS for {target}')
            temp.seek(0)
            with store.open(target, 'wb') as dest:
                while True:
                    chunk = temp.read(8192)
                    if len(chunk) == 0:  # eof
                        break
                    dest.write(chunk)
            logging.info(f'Upload to GCS complete for {target}')


def run(argv: t.List[str], save_main_session: bool = True):
    """Main entrypoint & pipeline definition."""
    parser = argparse.ArgumentParser(
        description='Weather Downloader downloads netcdf files from ECMWF to Google Cloud Storage.'
    )
    parser.add_argument('config', type=argparse.FileType('r', encoding='utf-8'),
                        help='path/to/config.cfg, specific to the <client>. Accepts *.cfg and *.json files.')
    parser.add_argument('-c', '--client', type=str, choices=CLIENTS.keys(), default=next(iter(CLIENTS.keys())),
                        help=f"Choose a weather API client; default is '{next(iter(CLIENTS.keys()))}'.")
    parser.add_argument('-f', '--force-download', action="store_true",
                        help="Force redownload of partitions that were previously downloaded.")
    parser.add_argument('-d', '--dry-run', action='store_true', default=False,
                        help='Run pipeline steps without _actually_ downloading or writing to cloud storage.')
    parser.add_argument('-m', '--manifest-location', type=Location, default='mock://',
                        help="Location of the manifest. Either a Firestore database URI, GCS bucket, or 'mock://' for "
                             "an in-memory location.")

    known_args, pipeline_args = parser.parse_known_args(argv[1:])

    configure_logger(2)  # 0 = error, 1 = warn, 2 = info, 3 = debug

    config = {}
    with known_args.config as f:
        config = process_config(f)

    config['parameters']['force_download'] = known_args.force_download
    config['parameters']['user_id'] = os.getlogin()

    # We use the save_main_session option because one or more DoFn's in this
    # workflow rely on global context (e.g., a module imported at module level).
    pipeline_options = PipelineOptions(pipeline_args)
    pipeline_options.view_as(SetupOptions).save_main_session = save_main_session

    client = CLIENTS[known_args.client](config)
    store = gcsio.GcsIO()
    config['parameters']['force_download'] = known_args.force_download
    manifest = parse_manifest_location(known_args.manifest_location)

    if known_args.dry_run:
        client = FakeClient(config)
        store = TempFileStore('dry_run')
        config['parameters']['force_download'] = True

    configured_fetch_data = functools.partial(fetch_data, client=client, manifest=manifest, store=store)

    with beam.Pipeline(options=pipeline_options) as p:
        (
                p
                | 'Create' >> beam.Create(prepare_partition(config, manifest=manifest, store=store))
                | 'FetchData' >> beam.Map(configured_fetch_data)
        )