# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import datetime
import unittest
from functools import wraps

import weather_mv
from .sinks import match_datetime, open_dataset


class TestDataBase(unittest.TestCase):
    def setUp(self) -> None:
        self.test_data_folder = f'{next(iter(weather_mv.__path__))}/test_data'


def _handle_missing_grib_be(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except ValueError as e:
            # Some setups may not have Cfgrib installed properly. Ignore tests for these cases.
            e_str = str(e)
            if "Consider explicitly selecting one of the installed engines" not in e_str or "cfgrib" in e_str:
                raise

    return decorated


class OpenDatasetTest(TestDataBase):

    def setUp(self) -> None:
        super().setUp()
        self.test_data_path = f'{self.test_data_folder}/test_data_20180101.nc'
        self.test_grib_path = f'{self.test_data_folder}/test_data_grib_single_timestep'
        self.test_tif_path = f'{self.test_data_folder}/test_data_tif_start_time.tif'

    def test_opens_grib_files(self):
        with open_dataset(self.test_grib_path) as ds1:
            self.assertIsNotNone(ds1)
            self.assertDictContainsSubset({'is_normalized': True}, ds1.attrs)
        with open_dataset(self.test_grib_path, disable_grib_schema_normalization=True) as ds2:
            self.assertIsNotNone(ds2)
            self.assertDictContainsSubset({'is_normalized': False}, ds2.attrs)

    def test_accepts_xarray_kwargs(self):
        with open_dataset(self.test_data_path) as ds1:
            self.assertIn('d2m', ds1)
            self.assertDictContainsSubset({'is_normalized': False}, ds1.attrs)
        with open_dataset(self.test_data_path, {'drop_variables': 'd2m'}) as ds2:
            self.assertNotIn('d2m', ds2)
            self.assertDictContainsSubset({'is_normalized': False}, ds2.attrs)

    def test_opens_tif_files(self):
        with open_dataset(self.test_tif_path, tif_metadata_for_datetime='start_time') as ds:
            self.assertIsNotNone(ds)
            self.assertDictContainsSubset({'is_normalized': False}, ds.attrs)


class DatetimeTest(unittest.TestCase):

    def test_datetime_regex_string(self):
        file_name = '3B-HHR-E_MS_MRG_3IMERG_20220901-S000000-E002959_0000_V06C_30min.tiff'

        regex_str = '3B-HHR-E_MS_MRG_3IMERG_%Y%m%d-S%H%M%S-*.tiff'

        expected = datetime.datetime.strptime('2022-09-01 00:00:00', '%Y-%m-%d %H:%M:%S')
        actual = match_datetime(file_name, regex_str)

        self.assertEqual(actual, expected)

    def test_datetime_regex_string_with_missing_parameters(self):
        file_name = '3B-HHR-E_MS_MRG_3IMERG_0901-S000000-E002959_0000_V06C_30min.tiff'

        regex_str = '3B-HHR-E_MS_MRG_3IMERG_%m%d-S%H%M%S-*.tiff'

        expected = datetime.datetime.strptime('1978-09-01 00:00:00', '%Y-%m-%d %H:%M:%S')
        actual = match_datetime(file_name, regex_str)

        self.assertEqual(actual, expected)

    def test_datetime_regex_string_with_different_order(self):
        file_name = '3B-HHR-E_MS_MRG_3IMERG_09012022-S000000-E002959_0000_V06C_30min.tiff'

        regex_str = '3B-HHR-E_MS_MRG_3IMERG_%m%d%Y-S%H%M%S-*.tiff'

        expected = datetime.datetime.strptime('2022-09-01 00:00:00', '%Y-%m-%d %H:%M:%S')
        actual = match_datetime(file_name, regex_str)

        self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
