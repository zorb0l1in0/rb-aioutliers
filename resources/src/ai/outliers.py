# Copyright (C) 2023 Eneo Tecnologia S.L.
#
# Authors :
# Miguel Álvarez Adsuara <malvarez@redborder.com>
# Pablo Rodriguez Flores <prodriguez@redborder.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as
# published by the Free Software Foundation, either version 2 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import os
'''
Start of important OS Variables
'''
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '1'
'''
End of important OS Variables
'''
import sys
import shutil
import numpy as np
import configparser
import pandas as pd
import tensorflow as tf

from resources.src.logger import logger

class Autoencoder:
    """
    Autoencoder class for anomaly detection.

    Args:
        model_file (str): Path to model .keras file.
        model_config (dict): Model parameters (metrics, timestamps, etc...)
    """
    def __init__(self, model_file, model_config_file):
        """
        Initializes the Autoencoder model and defines constants.

        Args:
            model_file (str): Path to model .keras file.
            model_config_file (str): Path to the model config, including:
                metrics (list): Names of the metrics used by the module.
                timestamp (list): Names of the timestamp columns used by the module.
                avg_loss (float): Average loss of the model.
                std_loss (float): Standard deviation of the loss of the model.
                window_size (int): Number of entries the model will put together in a 'window'.
                num_window (int): Number of windows the model will put together in each slice.
                loss_mult_metric (float): Extra penalty in the loss function for guessing wrong metrics.
                loss_mult_minute (float): Extra penalty in the loss function for guessing wrong
                  'minute' field.
        """
        try:
            self.check_existence(model_file, model_config_file)
        except FileNotFoundError as e:
            logger.logger.error("Could not find the asked model files")
            raise e
        try:
            model_config = configparser.ConfigParser()
            model_config.read(model_config_file)
            columns_section = model_config['Columns']
            self.metrics = columns_section.get('METRICS', '').split(', ')
            self.timestamp = columns_section.get('TIMESTAMP', '').split(', ')
            self.columns = self.metrics + self.timestamp
            general_section = model_config['General']
            self.avg_loss = float(general_section.get('AVG_LOSS', 0.0))
            self.std_loss = float(general_section.get('STD_LOSS', 0.0))
            self.window_size = int(general_section.get('WINDOW_SIZE', 0))
            self.num_window = int(general_section.get('NUM_WINDOWS', 0))
            self.loss_mult_metric = float(general_section.get('LOSS_MULT_METRIC', 0))
            self.loss_mult_minute = float(general_section.get('LOSS_MULT_MINUTE', 0))
        except Exception as e:
            logger.logger.error(f"Could not load model conif: {e}")
            raise e
        try:
            self.model = tf.keras.models.load_model(
                model_file,
                compile=False
            )
        except Exception as e:
            logger.logger.error(f"Could not load model {e}")
            raise e

    def check_existence(self, model_file, model_config_file):
        """
        Check existence of model files and copy them if missing.

        This function checks if the provided `model_file` and `model_config_file` exist in their
        respective paths. If they don't exist, it raises an error.

        Args:
            model_file (str): Path to the target model file.
                - The full path to the model file you want to check and potentially copy.
            model_config_file (str): Path to the target model configuration file.
                - The full path to the model configuration file you want to check and potentially copy.
        """
        if not os.path.exists(model_file):
            error_msg=f"Model file '{os.path.basename(model_file)}' not found"
            logger.logger.error(error_msg)
            raise FileNotFoundError(error_msg)
        if not os.path.exists(model_config_file):
            error_msg=f"Model config file '{os.path.basename(model_config_file)}' not found"
            logger.logger.error(error_msg)
            raise FileNotFoundError(error_msg)

    def rescale(self, data):
        """
        Rescale data between 0-1.
        For a metric x, the rescaling function is tanh(ln(x+1)/32).
        For the minute field, it is rescaled by dividing the number between 1440.

        Args:
            data (numpy.ndarray): Input data as a numpy array.

        Returns:
            (numpy.ndarray): Rescaled data as a numpy array.
        """
        num_metrics = len(self.metrics)
        rescaled=data.copy()
        rescaled[..., 0:num_metrics]=np.tanh(np.log1p(rescaled[..., 0:num_metrics])/32)
        rescaled[..., num_metrics]=rescaled[..., num_metrics]/1440
        return rescaled

    def descale(self, data):
        """
        Descale data to original scale.

        Args:
            data (numpy.ndarray): Input data as a numpy array.

        Returns:
            (numpy.ndarray): Descaled data as a numpy array.
        """
        num_metrics = len(self.metrics)
        descaled = data.copy()
        descaled = np.where(descaled > 1.0, 1.0, np.where(descaled < -1.0, -1.0, descaled))
        descaled[..., 0:num_metrics] = np.expm1(32*np.arctanh(descaled[..., 0:num_metrics]))
        descaled[..., num_metrics]=descaled[..., num_metrics]*1440
        return descaled

    def model_loss(self, y_true, y_pred, single_value=True):
        """
        Calculate the weighted loss for the model.
        Compares the input with boolean-valued tensors IS_METRIC and IS_MINUTE.
        Where IS_METRIC is true, the value of the input is multiplied by mult1,
        where IS_MINUTE is true, the value of the input is multiplied by mult2,
        otherwise, the value is left unchanged.
        Then, the difference between both tensors is evaluated and a log_cosh loss
        is applied.

        Args:
            y_true (tf.Tensor): True target values.
            y_pred (tf.Tensor): Predicted values.
            single_value (bool): Set to False to return a 3D array with the loss on each timestamp.

        Returns:
            (tf.Tensor): Weighted loss value or a 3D loss array.
        """
        y_true = tf.cast(y_true, tf.float16)
        y_pred = tf.cast(y_pred, tf.float16)
        num_metrics = len(self.metrics)
        num_features = len(self.columns)
        is_metric = (tf.range(num_features) < num_metrics)
        is_minute = (tf.range(num_features) == num_metrics)
        mult_true = tf.where(
            is_metric, self.loss_mult_metric * y_true,
            tf.where(is_minute, self.loss_mult_minute * y_true, y_true)
        )
        mult_pred = tf.where(
            is_metric, self.loss_mult_metric * y_pred,
            tf.where(is_minute, self.loss_mult_minute * y_pred, y_pred)
        )
        standard_loss = tf.math.log(tf.cosh((mult_true - mult_pred)))
        if single_value:
            standard_loss = tf.reduce_mean(standard_loss)
        return standard_loss

    def slice(self, data, index = []):
        """
        Transform a 2D numpy array into a 3D array readable by the model.

        Args:
            data (numpy.ndarray): 2D numpy array with the data to prepare.
            index (list): Index in case you want only some of the slices returned.

        Returns:
            (numpy.ndarray): 3D numpy array that can be processed by the model.
        """
        _l = len(data)
        sliced_data = []
        slice_length = self.window_size * self.num_window
        if len(index) == 0:
            index = np.arange(0, _l-slice_length+1 , self.window_size)
        for i in index:
            sliced_data.append(data[i:i+slice_length])
        return np.array(sliced_data)

    def flatten(self, data):
        """
        Flatten a 3D numpy array used by the model into a human-readable 2D numpy array.
        Args:
            data (numpy.ndarray): 3D numpy array.
        Returns:
            (numpy.ndarray): 2D numpy array with the natural format of the data.
        """
        tsr = data.copy()
        num_slices, slice_len, features = tsr.shape
        flattened_len = (num_slices-1)*self.window_size + slice_len
        flattened_tensor = np.zeros([flattened_len, features])
        scaling = np.zeros(flattened_len)
        for i in range(num_slices):
            left_pad = i*self.window_size
            right_pad = left_pad+slice_len
            flattened_tensor[left_pad:right_pad] += tsr[i]
            scaling[left_pad:right_pad] +=1
        flattened_tensor = flattened_tensor / scaling[:, np.newaxis]
        return flattened_tensor

    def calculate_predictions(self, data):
        """
        Proccesses the data, calculates the prediction and its loss.

        Args:
            data (numpy.ndarray): 2D numpy array with the relevant data.
        Returns:
            predicted (numpy.ndarray): predicted data
            anomalies (numpy.ndarray): anomalies detected
            loss (numpy.ndarray): loss function for each entry
        """
        prep_data = self.slice(self.rescale(data))
        predicted = self.model.predict(prep_data)
        loss = self.flatten(self.model_loss(prep_data, predicted, single_value = False).numpy())
        predicted = self.descale(self.flatten(predicted))
        return predicted, loss

    def compute_json(self, metric, raw_json):
        """
        Main method used for anomaly detection.

        Make the model process Json data and output to RedBorder prediction Json format.
        It includes the prediction for each timestamp and the anomalies detected.

        Args:
            metric (string): the name of field being analyzed.
            raw_json (dict): deserialized Json druid response with the data.

        Returns:
            (dict): deserialized Json with the anomalies and predictions for the data with RedBorder
              prediction Json format.
        """
        if metric=="" or metric not in self.metrics:
            error_msg = f"Model has not a metric called {metric}"
            logger.logger.error(error_msg)
            raise ValueError(error_msg)
        if not raw_json:
            error_msg = f"Input data is empty"
            logger.logger.error(error_msg)
            raise ValueError(error_msg)
        threshold = self.avg_loss+5*self.std_loss
        data, timestamps = self.input_json(raw_json)
        predicted, loss = self.calculate_predictions(data)
        predicted = pd.DataFrame(predicted, columns=self.columns)
        predicted['timestamp'] = timestamps
        anomalies = predicted[loss>threshold]
        return self.output_json(metric, anomalies, predicted)

    def granularity_from_dataframe(self, dataframe):
        """
        Extract the granularity from a dataframe. The granularity is suposed to be the difference
        between successive timestamps.

        Args:
            dataframe (pandas.DataFrame): Dataframe with timestamp column

        Returns:
            time_diffs (pandas.Series): Series with the estimated Granularity of the dataframe.
        """
        time_diffs = pd.to_datetime(dataframe["timestamp"]).diff().dt.total_seconds() // 60
        time_diffs.iloc[0] = time_diffs.iloc[1]
        time_diffs = time_diffs.where(time_diffs >= 0, time_diffs.shift(-1))
        return time_diffs

    def input_json(self, raw_json):
        """
        Transform Json data into numpy.ndarray readable by the model.
        Also returns the timestamps for each entry.

        Args:
            raw_json (dict): deserialized Json druid response with the data.

        Returns:
            data (numpy.ndarray): transformed data.
            timestamps (pandas.Series): pandas series with the timestamp of each entry.
        """
        data = pd.json_normalize(raw_json)
        data["granularity"] = self.granularity_from_dataframe(data)
        metrics_dict = {f"result.{metric}": metric for metric in self.metrics}
        data.rename(columns=metrics_dict, inplace=True)
        timestamps = data['timestamp'].copy()
        data['timestamp'] = pd.to_datetime(data['timestamp'])
        data['minute'] = data['timestamp'].dt.minute + 60 * data['timestamp'].dt.hour
        data['weekday']= data['timestamp'].dt.weekday
        data = pd.get_dummies(data, columns=['weekday'], prefix=['weekday'], drop_first=True)
        missing_columns = set(self.columns) - set(data.columns)
        data[list(missing_columns)] = 0
        data = data[self.columns].dropna().astype('float')
        data_array = data.values
        return data_array, timestamps

    def output_json(self, metric, anomalies, predicted):
        """
        Changes the format of the model's output to a JSON compatible with redBorder.

        Args:
            metric (string): the name of field being analyzed.
            anomalies (pandas.DataFrame): anomalies detected by the model.
            predicted (pandas.DataFrame): predictions made by the model.

        Returns:
            (dict): deserialized Json with the anomalies and predictions for the data with RedBorder prediction
              Json format.
        """
        predicted = predicted.copy()
        anomalies = anomalies.copy()
        predicted = predicted[[metric,'timestamp']].rename(columns={metric:"forecast"})
        anomalies = anomalies[[metric,'timestamp']].rename(columns={metric:"expected"})
        return  {
            "anomalies":anomalies.to_dict(orient="records"),
            "predicted":predicted.to_dict(orient="records"),
            "status": "success"
        }

    @staticmethod
    def execute_prediction_model(data, metric, model_file, model_config):
        try:
            autoencoder = Autoencoder(model_file, model_config)
            return autoencoder.compute_json(metric, data)
        except Exception as e:
            logger.logger.error("Couldn't execute model")
            return Autoencoder.return_error(e)
    @staticmethod
    def return_error(error="error"):
        """
        Returns an adequate formatted JSON for whenever there is an error.

        Args:
            error (string): message detailing what type of error has been fired.
        """
        return { "status": "error", "msg":error }