#!/usr/bin/env python3
# vim: ts=4 sw=4 expandtab
"""Machine learning model for disk failure prediction.

This classes defined here provide the disk failure prediction module.
RHDiskFailurePredictor uses the models developed at the AICoE in the
Office of the CTO at Red Hat. These models were built using the open
source Backblaze SMART metrics dataset.
PSDiskFailurePredictor uses the models developed by ProphetStor as an
example.

An instance of the predictor is initialized by providing the path to trained
models. Then, to predict hard drive health and deduce time to failure, the
predict function is called with 6 days worth of SMART data from the hard drive.
It will return a string to indicate disk failure status: "Good", "Warning",
"Bad", or "Unknown".

An example code is as follows:

>>> model = disk_failure_predictor.RHDiskFailurePredictor()
>>> status = model.initialize("./models")
>>> if status:
>>>     model.predict(device_data)
'Bad'
"""
import os
import pickle
import logging
import argparse

import numpy as np

import json
import sys
from enum import Enum


s_Reallocated_Sector_Count = '5'
s_Reported_Uncorrectable_Errors = '187'
s_Command_Timeout = '188'
s_Current_Pending_Sector_Count = '197'
s_Offline_Uncorrectable = '198'


def get_diskfailurepredictor_path():
    path = os.path.abspath(__file__)
    dir_path = os.path.dirname(path)
    return dir_path


class RHDiskFailurePredictor(object):
    """Disk failure prediction module developed at Red Hat

    This class implements a disk failure prediction module.
    """

    # json with manufacturer names as keys
    # and features used for prediction as values
    CONFIG_FILE = "config.json"
    PREDICTION_CLASSES = {-1: "Unknown", 0: "Good", 1: "Warning", 2: "Bad"}

    # model name prefixes to identify vendor
    MANUFACTURER_MODELNAME_PREFIXES = {
        "WD": "WDC",           # for cases like "WDxxx"
        "WDC": "WDC",
        "Toshiba": "Toshiba",  # for cases like "Toshiba xxx"
        "TOSHIBA": "Toshiba",  # for cases like "TOSHIBA xxx"
        "toshiba": "Toshiba",  # for cases like "toshiba xxx"
        "ST": "Seagate",       # for cases like "STxxxx"
        "Seagate": "Seagate",  # for cases like "Seagate BarraCuda ZAxxx"
        "ZA": "Seagate",       # for cases like "ZAxxxx"
        "Hitachi": "Hitachi",
        "HGST": "HGST",
    }

    LOGGER = logging.getLogger()

    def __init__(self):
        """
        This function may throw exception due to wrong file operation.
        """
        self.model_dirpath = ""
        self.model_context = {}

    def initialize(self, model_dirpath):
        """Initialize all models. Save paths of all trained model files to list

        Arguments:
            model_dirpath {str} -- path to directory of trained models

        Returns:
            str -- Error message. If all goes well, return None
        """
        # read config file as json, if it exists
        config_path = os.path.join(model_dirpath, self.CONFIG_FILE)
        if not os.path.isfile(config_path):
            return "Missing config file: " + config_path
        else:
            with open(config_path) as f_conf:
                self.model_context = json.load(f_conf)

        # ensure all manufacturers whose context is defined in config file
        # have models and scalers saved inside model_dirpath
        for manufacturer in self.model_context:
            scaler_path = os.path.join(model_dirpath, manufacturer + "_scaler.pkl")
            if not os.path.isfile(scaler_path):
                return "Missing scaler file: {}".format(scaler_path)
            model_path = os.path.join(model_dirpath, manufacturer + "_predictor.pkl")
            if not os.path.isfile(model_path):
                return "Missing model file: {}".format(model_path)

        self.model_dirpath = model_dirpath

    def __preprocess(self, device_data, manufacturer):
        """Scales and transforms input dataframe to feed it to prediction model

        Arguments:
            device_data {list} -- list in which each element is a dictionary with key,val
                                as feature name,value respectively.
                                e.g.[{'smart_1_raw': 0, 'user_capacity': 512 ...}, ...]
            manufacturer {str} -- manufacturer of the hard drive

        Returns:
            numpy.ndarray -- (n, d) shaped array of n days worth of data and d
                                features, scaled
        """
        # get the attributes that were used to train model for current manufacturer
        try:
            model_features = self.model_context[manufacturer]
            model_smart_attr = [i for i in model_features if 'smart' in i]
        except KeyError as e:
            RHDiskFailurePredictor.LOGGER.debug(
                "No context (SMART attributes on which model has been trained) found for manufacturer: {}".format(
                    manufacturer
                )
            )
            return None

        # assuming capacity does not change across days
        user_capacity = device_data['capacity_bytes']

        # what timestamps do we have the data from
        days = sorted(device_data['smart_data'].keys())

        # (n,d) shaped array to hold smart attribute values for device
        # where n is the number of days and d is number of SMART attributes
        device_smart_attr_values = np.empty(
            shape=(len(days), len(model_smart_attr))
        )
        for di, day in enumerate(days):
            for ai, attr in enumerate(model_smart_attr):
                if 'raw' in attr:
                    device_smart_attr_values[di][ai] = device_data['smart_data'][day]['attr'][attr.split('_')[1]]['val_raw']
                elif 'normalized' in attr:
                    device_smart_attr_values[di][ai] = device_data['smart_data'][day]['attr'][attr.split('_')[1]]['val_norm']
                elif 'user_capacity' in attr:
                    continue
                else:
                    raise ValueError('Unknown attribute found in config')

        # featurize n (6 to 12) days data - mean,std,coefficient of variation

        # rolling time window interval size in days
        roll_window_size = 6

        # extract rolling mean and std for SMART values
        means = np.empty(shape=(len(days) - roll_window_size + 1, device_smart_attr_values.shape[1]))
        stds = np.empty(shape=(len(days) - roll_window_size + 1, device_smart_attr_values.shape[1]))

        for i in range(0, device_smart_attr_values.shape[0] - roll_window_size + 1):
            means[i, :] = device_smart_attr_values[i: i+roll_window_size, :].mean(axis=0)
            stds[i, :] = device_smart_attr_values[i: i+roll_window_size, :].std(axis=0, ddof=1)
            
        # calculate coefficient of variation
        cvs = np.divide(stds, means, out=np.zeros_like(stds), where=means!=0)

        # combine all extracted features
        if 'user_capacity' in model_features:
            featurized = np.hstack((
                means,
                stds,
                cvs,
                np.repeat(user_capacity, len(days) - roll_window_size + 1).reshape(-1, 1),
            ))
        else:
            featurized = np.hstack((means, stds, cvs))

        # scale features
        scaler_path = os.path.join(self.model_dirpath, manufacturer + "_scaler.pkl")
        with open(scaler_path, 'rb') as f:
            scaler = pickle.load(f)
        featurized = scaler.transform(featurized)
        return featurized

    @staticmethod
    def __get_manufacturer(model_name):
        """Returns the manufacturer name for a given hard drive model name

        Arguments:
            model_name {str} -- hard drive model name

        Returns:
            str -- manufacturer name
        """
        for prefix, manufacturer in RHDiskFailurePredictor.MANUFACTURER_MODELNAME_PREFIXES.items():
            if model_name.startswith(prefix):
                return manufacturer
        # print error message
        RHDiskFailurePredictor.LOGGER.debug(
            "Could not infer manufacturer from model name {}".format(model_name)
        )

    def predict(self, device_data):
        # get manufacturer preferably as a smartctl attribute
        manufacturer = device_data.get("vendor", None)

        # if not available then try to infer using model name
        if manufacturer is None:
            RHDiskFailurePredictor.LOGGER.debug(
                '"vendor" field not found in smartctl output. Will try to infer manufacturer from model name.'
            )
            if "model" not in device_data.keys():
                RHDiskFailurePredictor.LOGGER.debug(
                    '"model" field not found in smartctl output.'
                )
            else:
                manufacturer = RHDiskFailurePredictor.__get_manufacturer(
                    device_data["model"]
                )

        # print error message and return Unknown
        if manufacturer is None:
            RHDiskFailurePredictor.LOGGER.debug(
                "Manufacturer could not be determiend. This may be because \
                DiskPredictor has never encountered this manufacturer before, \
                    or the model name is not according to the manufacturer's \
                        naming conventions known to DiskPredictor"
            )
            return RHDiskFailurePredictor.PREDICTION_CLASSES[-1]
        else:
            manufacturer = manufacturer.lower()

        # preprocess for feeding to model
        preprocessed_data = self.__preprocess(device_data, manufacturer)
        if preprocessed_data is None:
            return RHDiskFailurePredictor.PREDICTION_CLASSES[-1]

        # get model for current manufacturer
        model_path = os.path.join(
            self.model_dirpath, manufacturer + "_predictor.pkl"
        )
        with open(model_path, 'rb') as f:
            model = pickle.load(f)

        # use prediction for latest day as the output
        pred_class_id = model.predict(preprocessed_data)[-1]
        return RHDiskFailurePredictor.PREDICTION_CLASSES[pred_class_id]


class PreditionResult(Enum):
    GOOD = 0
    FAIL = 1

def simple_prediction(device_data):
    if len(device_data) == 0:
        return "Invalid prediction input"
    # Get most recent report
    report_date = sorted(device_data['smart_data'].keys(), reverse=True)[0]
    report = device_data['smart_data'][report_date]['attr']
    if s_Reallocated_Sector_Count in report and report[s_Reallocated_Sector_Count]['val_raw'] > 0:
        return PreditionResult.FAIL
    return PreditionResult.GOOD

def main():
    # args to decide which model to use for prediction
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--predictor-model', '--pm',
        type=str,
        choices=['redhat', 'simple'],
        default='redhat',
    )
    args = parser.parse_args()

    inp_json = sys.stdin.read()
    device_data = json.loads(inp_json)

    if args.predictor_model == 'simple':
        prediction_result = simple_prediction(device_data)
    elif args.predictor_model == 'redhat':
        # init model
        predictor = RHDiskFailurePredictor()
        predictor.initialize("{}/models/{}".format(get_diskfailurepredictor_path(), args.predictor_model))

        # make prediction
        prediction_result = predictor.predict(device_data)
    else:
        raise ValueError(f'Got invalid input for `--predictor-model`: {args.predictor_model}')

if __name__ == '__main__':
    sys.exit(main())

