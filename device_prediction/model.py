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
>>>     model.predict(disk_days)
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

    def __preprocess(self, disk_days, manufacturer):
        """Scales and transforms input dataframe to feed it to prediction model

        Arguments:
            disk_days {list} -- list in which each element is a dictionary with key,val
                                as feature name,value respectively.
                                e.g.[{'smart_1_raw': 0, 'user_capacity': 512 ...}, ...]
            manufacturer {str} -- manufacturer of the hard drive

        Returns:
            numpy.ndarray -- (n, d) shaped array of n days worth of data and d
                                features, scaled
        """
        # get the attributes that were used to train model for current manufacturer
        try:
            model_smart_attr = self.model_context[manufacturer]
        except KeyError as e:
            RHDiskFailurePredictor.LOGGER.debug(
                "No context (SMART attributes on which model has been trained) found for manufacturer: {}".format(
                    manufacturer
                )
            )
            return None

        # from the input json construct a list such that, each element is
        # a tuple representing SMART data from one day e.g. (60, 100, 500.2)
        days = sorted(disk_days['smart_data'].keys())
        values = []
        for day in days:
            curr_day_values = []
            for attr in model_smart_attr:
                if 'raw' in attr:
                    curr_day_values.append(
                        disk_days['smart_data'][day]['attr'][attr.split('_')[1]]['val_raw']
                    )
                elif 'normalized' in attr:
                    curr_day_values.append(
                        disk_days['smart_data'][day]['attr'][attr.split('_')[1]]['val_norm']
                    )
                elif 'user_capacity' in attr:
                    curr_day_values.append(
                        disk_days['capacity_bytes']
                    )
                else:
                    raise ValueError('Unknown attribute found in config')
            values.append(
                curr_day_values
            )

        # create a numpy structured array i.e. include "column names" and
        # "column dtypes" for the input matrix `values`
        try:
            struc_dtypes = [(attr, np.float64) for attr in model_smart_attr]
            disk_days_sa = np.array(values, dtype=struc_dtypes)
        except KeyError as e:
            RHDiskFailurePredictor.LOGGER.debug(
                "Mismatch in SMART attributes used to train model and SMART attributes available"
            )
            return None

        # view structured array as 2d array for applying rolling window transforms
        # do not include capacity_bytes in this. only use smart_attrs
        disk_days_attrs = disk_days_sa[[attr for attr in model_smart_attr if 'smart_' in attr]]\
                            .view(np.float64).reshape(disk_days_sa.shape + (-1,))

        # featurize n (6 to 12) days data - mean,std,coefficient of variation
        # current model is trained on 6 days of data because that is what will be
        # available at runtime

        # rolling time window interval size in days
        roll_window_size = 6

        # rolling means generator
        gen = (disk_days_attrs[i: i + roll_window_size, ...].mean(axis=0) \
                for i in range(0, disk_days_attrs.shape[0] - roll_window_size + 1))
        means = np.vstack(gen)

        # rolling stds generator
        gen = (disk_days_attrs[i: i + roll_window_size, ...].std(axis=0, ddof=1) \
                for i in range(0, disk_days_attrs.shape[0] - roll_window_size + 1))
        stds = np.vstack(gen)

        # coefficient of variation
        cvs = stds / means
        cvs[np.isnan(cvs)] = 0

        # combine all extracted features
        featurized = np.hstack((
                                means,
                                stds,
                                cvs,
                                disk_days_sa['user_capacity'][: disk_days_attrs.shape[0] - roll_window_size + 1].reshape(-1, 1)
                                ))

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

    def predict(self, disk_days):
        # get manufacturer preferably as a smartctl attribute
        manufacturer = disk_days.get("vendor", None)

        # if not available then try to infer using model name
        if manufacturer is None:
            RHDiskFailurePredictor.LOGGER.debug(
                '"vendor" field not found in smartctl output. Will try to infer manufacturer from model name.'
            )
            if "model" not in disk_days.keys():
                RHDiskFailurePredictor.LOGGER.debug(
                    '"model" field not found in smartctl output.'
                )
            else:
                manufacturer = RHDiskFailurePredictor.__get_manufacturer(
                    disk_days["model"]
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
        preprocessed_data = self.__preprocess(disk_days, manufacturer)
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
        choices=['redhat', 'prophetstor', 'dummy'],
        default='redhat',
    )
    args = parser.parse_args()

    inp_json = sys.stdin.read()
    # fname = 'input_samples/predict_1669.json'
    # with open(fname, 'rb') as f:
    #     device_data = json.load(f)

    if args.predictor_model == 'dummy':
        prediction_result = simple_prediction(device_data)
    elif args.predictor_model == 'redhat':
        # init model
        predictor = RHDiskFailurePredictor()
        predictor.initialize("{}/models/{}".format(get_diskfailurepredictor_path(), args.predictor_model))

        # make prediction
        prediction_result = predictor.predict(device_data)
    elif args.predictor_model == 'prophetstor':
        raise NotImplementedError("ProphetStor sample model has not been updated for use on Grafana dashboards")

    #print(prediction_result)

if __name__ == '__main__':
    sys.exit(main())

