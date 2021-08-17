import hub
from hub.api.info import load_info
from hub.core.storage.provider import StorageProvider
from hub.core.tensor import create_tensor, Tensor
from typing import Any, Callable, Dict, Optional, Union, Tuple, List, Sequence
from hub.htype import HTYPE_CONFIGURATIONS, DEFAULT_HTYPE, UNSPECIFIED
import numpy as np

from hub.core.meta.dataset_meta import DatasetMeta
from hub.core.index import Index
from hub.integrations import dataset_to_tensorflow
from hub.util.keys import (
    dataset_exists,
    get_dataset_info_key,
    get_dataset_meta_key,
    tensor_exists,
)
from hub.util.bugout_reporter import hub_reporter
from hub.util.exceptions import (
    CouldNotCreateNewDatasetException,
    InvalidKeyTypeError,
    MemoryDatasetCanNotBePickledError,
    PathNotEmptyException,
    ReadOnlyModeError,
    TensorAlreadyExistsError,
    TensorDoesNotExistError,
    InvalidTensorNameError,
)
from hub.client.client import HubBackendClient
from hub.client.log import logger
from hub.util.path import get_path_from_storage


class Dataset:
    def __init__(
        self,
        storage: StorageProvider,
        index: Index = None,
        read_only: bool = False,
        public: Optional[bool] = True,
        token: Optional[str] = None,
        verbose: bool = True,
    ):
        """Initializes a new or existing dataset.

        Args:
            storage (StorageProvider): The storage provider used to access the dataset.
            index (Index): The Index object restricting the view of this dataset's tensors.
            read_only (bool): Opens dataset in read only mode if this is passed as True. Defaults to False.
                Datasets stored on Hub cloud that your account does not have write access to will automatically open in read mode.
            public (bool, optional): Applied only if storage is Hub cloud storage and a new Dataset is being created. Defines if the dataset will have public access.
            token (str, optional): Activeloop token, used for fetching credentials for Hub datasets. This is optional, tokens are normally autogenerated.
            verbose (bool): If True, logs will be printed. Defaults to True.

        Raises:
            ValueError: If an existing local path is given, it must be a directory.
            ImproperDatasetInitialization: Exactly one argument out of 'path' and 'storage' needs to be specified.
                This is raised if none of them are specified or more than one are specifed.
            InvalidHubPathException: If a Hub cloud path (path starting with hub://) is specified and it isn't of the form hub://username/datasetname.
            AuthorizationException: If a Hub cloud path (path starting with hub://) is specified and the user doesn't have access to the dataset.
            PathNotEmptyException: If the path to the dataset doesn't contain a Hub dataset and is also not empty.
        """
        self._read_only = read_only
        # uniquely identifies dataset
        self.path = get_path_from_storage(storage)
        self.storage = storage
        self.index = index or Index()
        self.tensors: Dict[str, Tensor] = {}
        self._token = token
        self.public = public
        self.verbose = verbose

        self._set_derived_attributes()

    def __enter__(self):
        self.storage.autoflush = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.storage.autoflush = True
        self.flush()

    @property
    def num_samples(self) -> int:
        """Returns the length of the smallest tensor.
        Ignores any applied indexing and returns the total length.
        """
        return min(map(len, self.tensors.values()), default=0)

    def __len__(self):
        """Returns the length of the smallest tensor"""
        tensor_lengths = [len(tensor[self.index]) for tensor in self.tensors.values()]
        return min(tensor_lengths, default=0)

    def __getstate__(self) -> Dict[str, Any]:
        """Returns a dict that can be pickled and used to restore this dataset.

        Note:
            Pickling a dataset does not copy the dataset, it only saves attributes that can be used to restore the dataset.
            If you pickle a local dataset and try to access it on a machine that does not have the data present, the dataset will not work.
        """
        if self.path.startswith("mem://"):
            raise MemoryDatasetCanNotBePickledError
        return {
            "path": self.path,
            "_read_only": self.read_only,
            "index": self.index,
            "public": self.public,
            "storage": self.storage,
            "_token": self.token,
            "verbose": self.verbose,
        }

    def __setstate__(self, state: Dict[str, Any]):
        """Restores dataset from a pickled state.

        Args:
            state (dict): The pickled state used to restore the dataset.
        """
        self.__dict__.update(state)
        self.tensors = {}
        self._set_derived_attributes()

    def __getitem__(
        self,
        item: Union[
            str, int, slice, List[int], Tuple[Union[int, slice, Tuple[int]]], Index
        ],
    ):
        if isinstance(item, str):
            if item not in self.tensors:
                raise TensorDoesNotExistError(item)
            else:
                return self.tensors[item][self.index]
        elif isinstance(item, (int, slice, list, tuple, Index)):
            return Dataset(
                storage=self.storage,
                index=self.index[item],
                read_only=self.read_only,
                token=self._token,
                verbose=False,
            )
        else:
            raise InvalidKeyTypeError(item)

    @hub_reporter.record_call
    def create_tensor(
        self,
        name: str,
        htype: str = DEFAULT_HTYPE,
        dtype: Union[str, np.dtype, type] = UNSPECIFIED,
        sample_compression: str = UNSPECIFIED,
        chunk_compression: str = UNSPECIFIED,
        **kwargs,
    ):
        """Creates a new tensor in the dataset.

        Args:
            name (str): The name of the tensor to be created.
            htype (str): The class of data for the tensor.
                The defaults for other parameters are determined in terms of this value.
                For example, `htype="image"` would have `dtype` default to `uint8`.
                These defaults can be overridden by explicitly passing any of the other parameters to this function.
                May also modify the defaults for other parameters.
            dtype (str): Optionally override this tensor's `dtype`. All subsequent samples are required to have this `dtype`.
            sample_compression (str): All samples will be compressed in the provided format. If `None`, samples are uncompressed.
            chunk_compression (str): All chunks will be compressed in the provided format. If `None`, chunks are uncompressed.
            **kwargs: `htype` defaults can be overridden by passing any of the compatible parameters.
                To see all `htype`s and their correspondent arguments, check out `hub/htypes.py`.

        Returns:
            The new tensor, which can also be accessed by `self[name]`.

        Raises:
            TensorAlreadyExistsError: Duplicate tensors are not allowed.
            InvalidTensorNameError: If `name` is in dataset attributes.
            NotImplementedError: If trying to override `chunk_compression`.
        """

        if tensor_exists(name, self.storage):
            raise TensorAlreadyExistsError(name)
        if name in vars(self):
            raise InvalidTensorNameError(name)

        # Seperate meta and info

        htype_config = HTYPE_CONFIGURATIONS[htype].copy()
        info_keys = htype_config.pop("_info", [])
        info_kwargs = {}
        meta_kwargs = {}
        for k, v in kwargs.items():
            if k in info_keys:
                info_kwargs[k] = v
            else:
                meta_kwargs[k] = v

        # Set defaults
        for k in info_keys:
            if k not in info_kwargs:
                info_kwargs[k] = htype_config[k]

        if sample_compression not in (None, UNSPECIFIED) and chunk_compression not in (
            None,
            UNSPECIFIED,
        ):
            raise ValueError(
                "Sample compression and chunk compression are mutually exclusive."
            )

        create_tensor(
            name,
            self.storage,
            htype=htype,
            dtype=dtype,
            sample_compression=sample_compression,
            chunk_compression=chunk_compression,
            **meta_kwargs,
        )
        self.meta.tensors.append(name)
        self.storage.maybe_flush()
        tensor = Tensor(name, self.storage)  # type: ignore

        self.tensors[name] = tensor

        tensor.info.update(info_kwargs)

        return tensor

    @hub_reporter.record_call
    def create_tensor_like(self, name: str, source: "Tensor") -> "Tensor":
        """Copies the `source` tensor's meta information and creates a new tensor with it. No samples are copied, only the meta/info for the tensor is.

        Args:
            name (str): Name for the new tensor.
            source (Tensor): Tensor who's meta/info will be copied. May or may not be contained in the same dataset.

        Returns:
            Tensor: New Tensor object.
        """

        info = source.info.__getstate__().copy()
        meta = source.meta.__getstate__().copy()
        del meta["min_shape"]
        del meta["max_shape"]
        del meta["length"]
        del meta["version"]

        destination_tensor = self.create_tensor(
            name,
            **meta,
        )
        destination_tensor.info.update(info)

        return destination_tensor

    __getattr__ = __getitem__

    def __setattr__(self, name: str, value):
        if isinstance(value, (np.ndarray, np.generic)):
            raise TypeError(
                "Setting tensor attributes directly is not supported. To add a tensor, use the `create_tensor` method."
                + "To add data to a tensor, use the `append` and `extend` methods."
            )
        else:
            return super().__setattr__(name, value)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def _load_meta(self):
        meta_key = get_dataset_meta_key()

        if dataset_exists(self.storage):
            if self.verbose:
                logger.info(f"{self.path} loaded successfully.")
            self.meta = self.storage.get_cachable(meta_key, DatasetMeta)

            for tensor_name in self.meta.tensors:
                self.tensors[tensor_name] = Tensor(tensor_name, self.storage)

        elif len(self.storage) > 0:
            # dataset does not exist, but the path was not empty
            raise PathNotEmptyException

        else:
            if self.read_only:
                # cannot create a new dataset when in read_only mode.
                raise CouldNotCreateNewDatasetException(self.path)
            self.meta = DatasetMeta()
            self.storage[meta_key] = self.meta
            self.flush()
            if self.path.startswith("hub://"):
                self.client.create_dataset_entry(
                    self.org_id,
                    self.ds_name,
                    self.meta.__getstate__(),
                    public=self.public,
                )

    @property
    def read_only(self):
        return self._read_only

    @read_only.setter
    def read_only(self, value: bool):
        if value:
            self.storage.enable_readonly()
        else:
            self.storage.disable_readonly()
        self._read_only = value

    @hub_reporter.record_call
    def pytorch(
        self,
        transform: Optional[Callable] = None,
        tensors: Optional[Sequence[str]] = None,
        num_workers: int = 1,
        batch_size: Optional[int] = 1,
        drop_last: Optional[bool] = False,
        collate_fn: Optional[Callable] = None,
        pin_memory: Optional[bool] = False,
    ):
        """Converts the dataset into a pytorch Dataloader.

        Note:
            Pytorch does not support uint16, uint32, uint64 dtypes. These are implicitly type casted to int32, int64 and int64 respectively.
            This spins up it's own workers to fetch data.

        Args:
            transform (Callable, optional) : Transformation function to be applied to each sample.
            tensors (List, optional): Optionally provide a list of tensor names in the ordering that your training script expects. For example, if you have a dataset that has "image" and "label" tensors, if `tensors=["image", "label"]`, your training script should expect each batch will be provided as a tuple of (image, label).
            num_workers (int): The number of workers to use for fetching data in parallel.
            batch_size (int, optional): Number of samples per batch to load. Default value is 1.
            drop_last (bool, optional): Set to True to drop the last incomplete batch, if the dataset size is not divisible by the batch size.
                If False and the size of dataset is not divisible by the batch size, then the last batch will be smaller. Default value is False.
                Read torch.utils.data.DataLoader docs for more details.
            collate_fn (Callable, optional): merges a list of samples to form a mini-batch of Tensor(s). Used when using batched loading from a map-style dataset.
                Read torch.utils.data.DataLoader docs for more details.
            pin_memory (bool, optional): If True, the data loader will copy Tensors into CUDA pinned memory before returning them. Default value is False.
                Read torch.utils.data.DataLoader docs for more details.

        Returns:
            A torch.utils.data.DataLoader object.
        """
        from hub.integrations import dataset_to_pytorch

        return dataset_to_pytorch(
            self,
            transform,
            tensors,
            num_workers=num_workers,
            batch_size=batch_size,
            drop_last=drop_last,
            collate_fn=collate_fn,
            pin_memory=pin_memory,
        )

    def _get_total_meta(self):
        """Returns tensor metas all together"""
        return {
            tensor_key: tensor_value.meta
            for tensor_key, tensor_value in self.tensors.items()
        }

    def _set_derived_attributes(self):
        """Sets derived attributes during init and unpickling."""

        self.storage.autoflush = True
        if self.path.startswith("hub://"):
            split_path = self.path.split("/")
            self.org_id, self.ds_name = split_path[2], split_path[3]
            self.client = HubBackendClient(token=self._token)

        self._load_meta()  # TODO: use the same scheme as `load_info`
        self.info = load_info(get_dataset_info_key(), self.storage)  # type: ignore
        self.index.validate(self.num_samples)

    @hub_reporter.record_call
    def tensorflow(self):
        """Converts the dataset into a tensorflow compatible format.

        See:
            https://www.tensorflow.org/api_docs/python/tf/data/Dataset

        Returns:
            tf.data.Dataset object that can be used for tensorflow training.
        """
        return dataset_to_tensorflow(self)

    def flush(self):
        """Necessary operation after writes if caches are being used.
        Writes all the dirty data from the cache layers (if any) to the underlying storage.
        Here dirty data corresponds to data that has been changed/assigned and but hasn't yet been sent to the
        underlying storage.
        """
        self.storage.flush()

    def clear_cache(self):
        """Flushes (see Dataset.flush documentation) the contents of the cache layers (if any) and then deletes contents
         of all the layers of it.
        This doesn't delete data from the actual storage.
        This is useful if you have multiple datasets with memory caches open, taking up too much RAM.
        Also useful when local cache is no longer needed for certain datasets and is taking up storage space.
        """
        if hasattr(self.storage, "clear_cache"):
            self.storage.clear_cache()

    def size_approx(self):
        """Estimates the size in bytes of the dataset.
        Includes only content, so will generally return an under-estimate.
        """
        tensors = self.tensors.values()
        chunk_engines = [tensor.chunk_engine for tensor in tensors]
        size = sum(c.num_chunks * c.min_chunk_size for c in chunk_engines)
        return size

    @hub_reporter.record_call
    def delete(self, large_ok=False):
        """Deletes the entire dataset from the cache layers (if any) and the underlying storage.
        This is an IRREVERSIBLE operation. Data once deleted can not be recovered.

        Args:
            large_ok (bool): Delete datasets larger than 1GB. Disabled by default.
        """
        if not large_ok:
            size = self.size_approx()
            if size > hub.constants.DELETE_SAFETY_SIZE:
                logger.info(
                    f"Hub Dataset {self.path} was too large to delete. Try again with large_ok=True."
                )
                return

        self.storage.clear()
        if self.path.startswith("hub://"):
            self.client.delete_dataset_entry(self.org_id, self.ds_name)
            logger.info(f"Hub Dataset {self.path} successfully deleted.")

    @staticmethod
    def from_path(path: str):
        """Creates a hub dataset from unstructured data.

        Note:
            This copies the data into hub format.
            Be careful when using this with large datasets.

        Args:
            path (str): Path to the data to be converted

        Returns:
            A Dataset instance whose path points to the hub formatted
            copy of the data.

        Raises:
            NotImplementedError: TODO.
        """

        raise NotImplementedError(
            "Automatic dataset ingestion is not yet supported."
        )  # TODO: hub.auto
        return None

    def __str__(self):
        path_str = ""
        if self.path:
            path_str = f"path='{self.path}', "

        mode_str = ""
        if self.read_only:
            mode_str = f"read_only=True, "

        index_str = f"index={self.index}, "
        if self.index.is_trivial():
            index_str = ""

        return f"Dataset({path_str}{mode_str}{index_str}tensors={self.meta.tensors})"

    __repr__ = __str__

    @property
    def token(self):
        """Get attached token of the dataset"""
        if self._token is None and self.path.startswith("hub://"):
            self._token = self.client.get_token()
        return self._token
