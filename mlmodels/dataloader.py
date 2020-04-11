import os
import sys
import inspect
from urllib.parse import urlparse
import tensorflow as tf
import torch
import torchtext
import pandas as pd
import numpy as np
import sklearn
import keras
from sklearn.model_selection import train_test_split
from cli_code.cli_download import Downloader
from collections.abc import MutableMapping
import json
from importlib import import_module
import cloudpickle as pickle
from preprocessor import Preprocessor
from util import load_callable_from_dict
import tensorflow.data


class DataLoaderError(Exception):
    pass


class MissingLocationKeyError(DataLoaderError):
    def __init__(self):
        print("Location key missing from the input dictionary.")


class UndeterminableLocationTypeError(DataLoaderError):
    def __init__(self, location):
        print(f"Location type cannot be inferred for '{location}'.")


class UnknownLocationTypeError(DataLoaderError):
    def __init__(self, path_type):
        print(f"Location type '{  path_type}' is unknown.")


class NonfileURLError(DataLoaderError):
    def __init__(self):
        print(f"URL must point to a file.")


class UndeterminableDataLoaderError(DataLoaderError):
    def __init__(self):
        print(
            f"""Loader function to be used was not provided and could not be
             automatically inferred from file type."""
        )


class NonIntegerBatchSizeError(DataLoaderError):
    def __init__(self):
        print(f"Provided batch size cannot be interpreted as an integer.")


class InvalidDataLoaderFunctionError(DataLoaderError):
    def __init__(self, loader):
        print(f"Invalid data loader function '{loader}\ specified.")


class NumpyGeneratorError(DataLoaderError):
    def __init__(self):
        print(f"Loading Numpy binaries as generators is unsupported.")


class OutputShapeError(DataLoaderError):
    def __init__(self, specified, actual):
        print(
            f"""Specified output shape {specified} does not match actual output
            shape {actual}"""
        )


def open_read(file):
    return open(f).read()


def pickle_load(file):
    return pickle.load(open(f, " r"))


def image_dir_load(path):
    return ImageDataGenerator().flow_from_directory(path)


def batch_generator(iterable, n=1):
    l = len(iterable)
    for ndx in range(0, l, n):
        yield iterable[ndx : min(ndx + n, l)]


class DataLoader:

    default_loaders = {
        ".csv": {"uri": "pandas::read_csv"},
        ".txt": {"uri": "dataloader::open_read"},
        ".npy": {"uri": "numpy::load"},
        ".npz": {"uri": "np::.load", "arg": {"allow_pickle": True}},
        ".pkl": {"uri": "dataloader::pickle_load"},
        "image_dir": {"uri": "dataloader::image_dir_load"},
    }

    def __init__(self, input_pars, loader, preprocessor, output, **args):
        self.intermediate_output = None
        self.intermediate_output_split = None
        self.final_output = None
        self.final_output_split = None

        self._misc_dict = args if args is not None else {}

        self._interpret_input_pars(input_pars)
        loaded_data = self._load_data(loader)
        if isinstance(preprocessor, Preprocessor):
            self.preprocessor = preprocessor
            processed_data = self.preprocessor.transform(loaded_data)
        else:
            self.preprocessor = Preprocessor(preprocessor)
            processed_data = self.preprocessor.fit_transform(loaded_data)
        self.intermediate_output = processed_data
        if self._names is not None:
            self.intermediate_output = self._name_outputs(
                self.names, self.intermediate_output
            )
        if self._split_data():
            self.final_output_split = tuple(
                self._interpret_output(output, o)
                for o in self.intermediate_output_split[0:2]
            ) + tuple(self.intermediate_output_split[2])
        else:
            self.final_output = self._interpret_output(output, self.intermediate_output)

    def __getitem__(self, key):
        return self._misc_dict[key]

    def _interpret_input_pars(self, input_pars):
        try:
            path = input_pars["path"]
        except KeyError:
            raise MissingLocationKeyError()

        path_type = input_pars.get("path_type", None)
        if path_type is None:
            if os.path.isfile(path):
                path_type = "file"
            if os.path.isdir(path):
                path_type = "dir"
            if urlparse(path).scheme != "":
                path_type = "url"
                download_path = input_pars.get("download_path", "./")
            if path_type == "dropbox":
                dropbox_download(path)
                path_type = "file"
            if path_type is None:
                raise UndeterminableLocationTypeError(path)

        elif path_type != "file" and path_type != "dir" and path_type != "url":
            raise UnknownLocationTypeError()

        file_type = input_pars.get("file_type", None)
        if file_type is None:
            if path_type == "dir":
                file_type = "image_dir"
            elif path_type == "file":
                file_type = os.path.splitext(path)[1]
            else:
                if path[-1] == "/":
                    raise NonfileURLError()
                file_type = os.path.splittext(path.split("/")[-1])[1]

        self.path = path
        self.path_type = path_type
        self.file_type = file_type
        self.test_size = input_pars.get("test_size", None)
        self.generator = input_pars.get("generator", False)
        if self.generator:
            try:
                self.batch_size = int(input_pars.get("batch_size", 1))
            except:
                raise NonIntegerBatchSizeError()
        self._names = input_pars.get("names", None)
        validation_split_function = [
            {"uri": "sklearn.model_selection::train_test_split", "args": {}},
            "test_size",
        ]
        self.validation_split_function = input_pars.get(
            "split_function", validation_split_function
        )
        self.split_outputs = input_pars.get("split_outputs", None)
        self.misc_outputs = input_pars.get("misc_outputs", None)

    def _load_data(self, loader):
        data_loader = loader.get("data_loader", None)
        if isinstance(data_loader, tuple):
            loader_function = data_loader[0]
            loader_args = data_loader[1]
        else:
            if data_loader is None or "uri" not in data_loader.keys():
                try:
                    if data_loader is not None and "arg" in data_loader.keys():
                        loader_args = data_loader["arg"]
                    else:
                        loader_args = {}
                    data_loader = self.default_loaders[self.file_type]
                except KeyError:
                    raise UndeterminableDataLoaderError()
            try:
                loader_function, args = load_callable_from_dict(data_loader)
                if args is not None:
                    loader_args.update(args)
                assert callable(loader_function)
            except:
                raise InvalidDataLoaderFunctionError(data_loader)

        if self.path_type == "file":
            if self.generator:
                if self.file_type == "csv":
                    if loader_function == pd.read_csv:
                        loader_args["chunksize"] = loader.get(
                            "chunksize", self.batch_size
                        )
            loader_arg = self.path

        if self.path_type == "url":
            if self.file_type == "csv" and loader_function == pd.read_csv:
                data = loader_function(self.path, **loader_args)
            else:
                downloader = Downloader(url)
                downloader.download(out_path)
                filename = self.path.split("/")[-1]
                loader_arg = out_path + "/" + filename
        data = loader_function(loader_arg, **loader_args)
        if self.file_type == "npz" and loader_function == np.load:
            data = [data[f] for f in data.files]

        return data

    def _interpret_output(self, output, intermediate_output):
        if isinstance(intermediate_output, list) and len(output) == 1:
            intermediate_output = intermediate_output[0]
        # case 0: non-tuple, non-dict: single output from the preprocessor/loader.
        # case 1: tuple of non-dicts: multiple outputs from the preprocessor/loader.
        # case 2: tuple of dicts: multiple args from the preprocessor/loader.
        # case 3: dict of non-dicts: multiple named outputs from the preprocessor/loader.
        # case 4: dict of dicts: multiple named dictionary outputs from the preprocessor. (Special case)
        case = 0
        if isinstance(intermediate_output, tuple):
            if not isinstance(intermediate_output[0], dict):
                case = 1
            else:
                case = 2
        if isinstance(intermediate_output, dict):
            if not isinstance(tuple(intermediate_output.values())[0], dict):
                case = 3
            else:
                case = 4
        
        #max_len enforcement
        max_len = output.get("out_max_len", None)
        try:
            if case == 0:
                intermediate_output = intermediate_output[0:max_len]
            if case == 1:
                intermediate_output = [o[0:max_len] for o in intermediate_output]
            if case == 3:
                intermediate_output = {
                    k: v[0:max_len] for k, v in intermediate_output.items()
                }
        except:
            pass

        # shape check
        shape = output.get("shape", None)
        if shape is not None:
            if (
                case == 0
                and hasattr(intermediate_output, "shape")
                and tuple(shape) != intermediate_output.shape
            ):
                raise OutputShapeError(tuple(shape), intermediate_output.shape[1:])
            if case == 1:
                for s, o in zip(shape, intermediate_output):
                    if hasattr(o, "shape") and tuple(s) != o.shape[1:]:
                        raise OutputShapeError(tuple(s), o.shape[1:])
            if case == 3:
                for s, o in zip(shape, tuple(intermediate_output.values())):
                    if hasattr(o, "shape") and tuple(s) != o.shape[1:]:
                        raise OutputShapeError(tuple(s), o.shape[1:])
        self.output_shape = shape

        # saving the intermediate output
        path = output.get("path", None)
        if isinstance(path, str):
            if isinstance(intermediate_output, np.ndarray):
                np.save(path, intermediate_output)
            elif isinstance(intermediate_output, pd.core.frame.DataFrame):
                intermediate_output.to_csv(path)
            elif isinstance(intermediate_output, tuple) and all(
                [isinstance(x, np.ndarray) for x in intermediate_output]
            ):
                np.savez(path, *intermediate_output)
            elif isinstance(intermediate_output, dict) and all(
                [isinstance(x, np.ndarray) for x in tuple(intermediate_output.values())]
            ):
                np.savez(path, *(tuple(intermediate_output.values)))
            else:
                pickle.dump(intermediate_output, open(path, "wb"))

        elif isinstance(path, list):
            try:
                for p, f in zip(path, intermediate_output):
                    if isinstance(f, np.ndarray):
                        np.save(p, self.f)
                    elif isinstance(f, pd.core.frame.DataFrame):
                        f.to_csv(f)
                    elif isinstance(f, list) and all(
                        [isinstance(x, np.ndarray) for x in f]
                    ):
                        np.savez(p, *f)
                    else:
                        pickle.dump(f, open(p, "wb"))
            except:
                pass

        # Framework-specific output formatting.
        final_output = intermediate_output
        output_format = output.get("format", None)
        if output_format == "tfDataset":
            if case == 3:
                intermediate_output = tuple(
                    x for x in tuple(intermediate_output.values())
                )
            if case == 2 or case == 4:
                raise Exception(
                    "Input format not supported for the specified output format"
                )
            final_output = tf.data.Dataset.from_tensor_slices(intermediate_output)
        if output_format == "tchDataset":
            if case == 3:
                intermediate_output = tuple(
                    x for x in tuple(intermediate_output.values())
                )
            if case == 2 or case == 4:
                raise Exception(
                    "Input format not supported for the specified output format"
                )
            if case == 1:
                final_output = torch.utils.data.TensorDataset(intermediate_output)
            else:
                final_output = torch.utils.data.TensorDataset(*intermediate_output)
        if output_format == "generic_generator":
            if case == 0:
                final_output = batch_generator(intermediate_output, self.batch_size)
            if case == 1:
                final_output = batch_generator(
                    tuple(zip(*intermediate_output)), self.batch_size
                )
            if case == 3:
                final_output = batch_generator(
                    tuple(zip(*tuple(intermediate_output.values()))), self.batch_size
                )
            if case == 2 or case == 4:
                raise Exception(
                    "Input format not supported for the specified output format"
                )

        return final_output

    def get_data(self, intermediate=False):
        if intermediate or self.final_output is None:
            if self.intermediate_output_split is not None:
                return (
                    *self.intermediate_output_split[0],
                    *self.intermediate_output_split[1],
                    *self.intermediate_output_split[2],
                )
            if isinstance(self.intermediate_output, dict):
                return tuple(self.intermediate_output.values())
            return self.intermediate_output
        if self.final_output_split is not None:
            return (
                *self.final_output_split[0],
                *self.final_output_split[1],
                *self.final_output_split[2],
            )
        return self.final_output

    def _name_outputs(self, names, outputs):
        if hasattr(outputs, "__getitem__") and len(outputs) == len(names):
            data = dict(zip(names, outputs))
            self._misc_dict.update(data)
            return data
        else:
            raise Exception("Outputs could not be named")

    def _split_data(self):
        if self.split_outputs is not None:
            if (
                self._names is not None or isinstance(self.intermediate_output, dict)
            ) or isinstance(self.intermediate_output, tuple):
                processed_data = tuple(
                    self.intermediate_output[n] for n in self.split_outputs
                )
        else:
            processed_data = self.intermediate_output
        func_dir = self.validation_split_function[0]
        split_size_arg_dict = {
            self.validation_split_function[1]: self.test_size,
            **func_dir.get("arg", {}),
        }
        if self.test_size > 0:
            func, arg = load_callable_from_dict(self.validation_split_function[0])
            if arg is None:
                arg = {}
            arg.update({self.validation_split_function[1]: self.test_size})
            l = len(processed_data)
            processed_data = func(*processed_data, **arg)
            processed_data_train = processed_data[0:l]
            processed_data_test = processed_data[l:]
            processed_data_misc = []
            if self._names is not None and isinstance(self.intermediate_output, dict):
                new_names = [x + "_train" for x in self.split_outputs]
                processed_data_train = dict(zip(new_names, processed_data_train))
                new_names = [x + "_test" for x in self.split_outputs]
                processed_data_test = dict(zip(new_names, processed_data_test))
            if self.misc_outputs is not None:
                if self._names is not None and isinstance(
                    self.intermediate_output, dict
                ):
                    processed_data_misc = {
                        misc: self.intermediate_output[misc]
                        for misc in self.misc_outputs
                    }
                else:
                    processed_data_misc = tuple(
                        self.intermediate_output[misc] for misc in self.misc_outputs
                    )
            self.intermediate_output_split = (
                processed_data_train,
                processed_data_test,
                processed_data_misc,
            )
            return True
        return False


if __name__ == "__main__":
    from models import test_module

    param_pars = {
        "choice": "json",
        "config_mode": "test",
        "data_path": "dataset/json_/03_nbeats.json",
    }
    test_module("model_tch/03_nbeats_dataloader.py", param_pars)
    # param_pars = {
    #   "choice": "json",
    #   "config_mode": "test",
    #   "data_path": f"dataset/json_/namentity_crm_bilstm_dataloader.json",
    # }
    #
    # test_module("model_keras/namentity_crm_bilstm_dataloader.py", param_pars)

    # param_pars = {
    #    "choice": "json",
    #    "config_mode": "test",
    #    "data_path": f"dataset/json_/textcnn_dataloader.json",
    # }
    # test_module("model_tch/textcnn_dataloader.py", param_pars)
