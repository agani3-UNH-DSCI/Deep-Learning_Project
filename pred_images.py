import importlib
import logging
import os
from os.path import join
import pickle as pkl
import sys

import h5py
from sacred import Experiment
import torch
import torch.nn as nn
import torch.nn.functional as f
from torch.utils.data import DataLoader, Subset

from configuration import CONFIG, datasets, models
from src.imageaugmentations import Compose, Normalize, ToTensor
from src.log_utils import log_config

ex = Experiment("pred_images")

log = logging.getLogger()
log.handlers = []

log_format = logging.Formatter(
    "%(asctime)s || %(name)s - [%(levelname)s] - %(message)s"
)

streamhandler = logging.StreamHandler(sys.stdout)
streamhandler.setFormatter(log_format)
log.addHandler(streamhandler)

log.setLevel("INFO")
ex.logger = log


@ex.capture
def pred(net, image, args):
    image = image.cuda(args["gpu"])
    with torch.no_grad():
        out = net(image)
    out = out.data.cpu()
    out = f.softmax(out, 1)
    return out


def save(out, lbl, image_path, ind, input_dir):
    with h5py.File(join(input_dir, "input{}.hdf5".format(ind)), "w") as file:
        file.create_dataset(
            "probabilities", data=out.squeeze().permute(1, 2, 0).numpy()
        )
        file.create_dataset("prediction", data=out.argmax(1).squeeze().numpy())
        file.create_dataset("ground_truths", data=lbl.squeeze().numpy())
        file.create_dataset("image_path", data=[image_path[0].encode("utf8")])


@ex.capture
def load_net_weights(net: nn.Module, filepath: str, _log) -> None:
    try:
        model_checkpoint = torch.load(filepath)["state_dict"]
    except KeyError:
        model_checkpoint = torch.load(filepath)
    except FileNotFoundError as e:
        raise FileNotFoundError(
            "The specified model weight file '{}' could not be found!".format(filepath)
        ) from e
    except Exception as e:
        raise e

    key_errors = net.load_state_dict(model_checkpoint, strict=False)
    if len(key_errors.missing_keys) == 0:
        if len(key_errors.unexpected_keys) != 0:
            _log.warning(
                (
                    "The following unexpected parameters were not "
                    "loaded into the model: "
                    "{}".format(key_errors.unexpected_keys)
                )
            )
    else:
        raise RuntimeError(
            "Missing keys in state dict:\n{}".format(key_errors.missing_keys)
        )


@ex.capture
def load_net_and_data(args, _log):
    """This functions loads the image data and the semantic segmentation network"""

    # DataParallel is needed due to weight loading!
    net = nn.DataParallel(
        getattr(
            importlib.import_module(models[args["model_name"]].module_name),
            models[args["model_name"]].class_name,
        )(**models[args["model_name"]].kwargs),
        device_ids=[args["gpu"]],
    )
    try:
        # try to load the weights into the DataParallel Module
        load_net_weights(net, models[args["model_name"]].model_weights)
    except RuntimeError:
        # If model weights were not saved while being in the DataParallel Module we can
        # try to load the weights directly into the network
        _log.debug(
            "Model weights could not be loaded into the DataParallel Module. "
            "Trying to load them into the network directly."
        )
        net = getattr(
            importlib.import_module(models[args["model_name"]].module_name),
            models[args["model_name"]].class_name,
        )(**models[args["model_name"]].kwargs)
        load_net_weights(net, models[args["model_name"]].model_weights)
    except Exception as e:
        raise e

    _log.info("Loading of model weights successful")
    net.eval()
    net = net.cuda(args["gpu"])

    traindat_module = importlib.import_module(CONFIG.TRAIN_DATASET.module_name)
    try:
        mean = getattr(traindat_module, "mean")
    except AttributeError as err:
        raise AttributeError(
            "Could not find a data mean for normalization in your dataset module."
        ) from err

    try:
        std = getattr(traindat_module, "std")
    except AttributeError as err:
        raise AttributeError(
            (
                "Could not find a data standard deviation for normalization "
                "in your dataset module."
            )
        ) from err

    trans = Compose([ToTensor(), Normalize(mean, std)])
    dat = getattr(
        importlib.import_module(datasets[args["dataset"]].module_name),
        datasets[args["dataset"]].class_name,
    )(
        transform=trans,
        **datasets[args["dataset"]].kwargs,
    )

    if args["classindex"] is not None and args["dataset"] == "a2d2":
        # get indices of the images with that specific class and subsample the dataset
        with open(args["a2d2_dataset_overview"], "rb") as file:
            img_inds = pkl.load(file)

        img_inds = img_inds[args.classindex]
        dat = Subset(dat, img_inds)
    elif args["dataset"] == "a2d2":
        # only load a subset of a2d2. This subset is saved in a file for later reloading
        with open("a2d2_random_selection.p", "rb") as file:
            img_inds = pkl.load(file)
        dat = torch.utils.data.Subset(dat, img_inds)
    else:
        img_inds = list(range(len(dat)))

    datloader = DataLoader(dat, batch_size=1, num_workers=args["num_cores"])

    return net, datloader, img_inds


@ex.config
def config():
    args = dict(
        dataset=CONFIG.DATASET.name,
        input_dir=CONFIG.INPUT_DIR,
        model_name=CONFIG.MODEL_NAME,
        classindex=CONFIG.CLASSINDEX,
        pretrained_model=models[CONFIG.MODEL_NAME].model_weights,
        num_cores=CONFIG.NUM_CORES,
        gpu=CONFIG.GPU_ID,
    )

    os.makedirs(args["input_dir"], exist_ok=True)

    if args["dataset"] == "a2d2":
        args["a2d2_dataset_overview"] = "a2d2_dataset_overview.p"


@ex.automain
def main(args, _run, _log):
    log_config(_run, _log)
    # load model and data:
    log.info("Loading network and dataset")
    net, datloader, img_inds = load_net_and_data(args)

    if args["classindex"] is not None:
        log.info(
            "Specified class: {} // Number of images containing this class: {}".format(
                args["classindex"], len(img_inds)
            )
        )

    # predict all images and save outputs together with filename and
    # annotation to hdf5 file:
    log.info("Predicting images...")
    n = len(datloader)
    log.debug("Total number of batches to process: {}".format(n))
    for i, (img, lbl, image_path) in enumerate(datloader):
        out = pred(net, img)
        save(out, lbl, image_path, img_inds[i], input_dir=args["input_dir"])
        if ((i + 1) % 1) == 0:
            log.info("\t\t Image {}/{}".format(i + 1, n))
