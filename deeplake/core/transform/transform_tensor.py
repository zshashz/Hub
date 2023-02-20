from deeplake.core.linked_tiled_sample import LinkedTiledSample
from deeplake.util.exceptions import TensorDoesNotExistError
from deeplake.core.partial_sample import PartialSample
from deeplake.core.linked_sample import LinkedSample
from deeplake.core.sample import Sample
from deeplake.core.tensor import Tensor
from typing import Union, List, Any
from itertools import chain

import numpy as np

import posixpath
import bisect


class TransformTensor:
    def __init__(self, dataset, name, is_group=False):
        self.items = []
        self.dataset = dataset
        self.name = name
        self.is_group = is_group
        self.idx = slice(None, None, None)
        self.numpy_only = True
        self.cum_sizes = []

    def __len__(self):
        if self.numpy_only:
            return 0 if not self.cum_sizes else self.cum_sizes[-1]
        return len(self.items)

    def __getattr__(self, item):
        return self.dataset[posixpath.join(self.name, item)][self.idx]

    def __getitem__(self, item):
        if isinstance(item, str):
            return self.__getattr__(item)
        self.idx = item
        return self

    def numpy(self) -> Union[List, np.ndarray]:
        if self.numpy_only:
            return self.numpy_compressed()

        if isinstance(self.idx, int):
            items = [self.numpy_compressed()]
            squeeze = True
        else:
            items = self.numpy_compressed()
            squeeze = False

        values: List[Any] = []
        for item in items:
            if isinstance(item, Sample):
                values.append(item.array)
            elif not isinstance(
                item,
                (LinkedSample, Tensor, type(None), PartialSample, LinkedTiledSample),
            ):
                values.append(np.asarray(item))
            else:
                values.append(item)
        if squeeze:
            values = values[0]
        return values

    def numpy_compressed(self):
        idx = self.idx
        if self.numpy_only:
            if isinstance(idx, int):
                i = bisect.bisect_right(self.cum_sizes, idx)
                if i == 0:
                    j = idx
                else:
                    j = idx - self.cum_sizes[i - 1]
                return self.items[i][j]
        return self.items[idx]

    def non_numpy_only(self):
        if self.numpy_only:
            items = list(chain(*self.items[:]))
            self.items.clear()
            self.items += items
            self.cum_sizes.clear()
            self.numpy_only = False

    def append(self, item):
        if self.is_group:
            raise TensorDoesNotExistError(self.name)
        if self.numpy_only:
            # optimization applicable only if extending
            self.non_numpy_only()
        self.items.append(item)
        if self.dataset.all_chunk_engines:
            self.dataset.item_added(item)

    def extend(self, items):
        if self.numpy_only:
            if isinstance(items, np.ndarray):
                self.items.append(items)
                if len(self.cum_sizes) == 0:
                    self.cum_sizes.append(len(items))
                else:
                    self.cum_sizes.append(self.cum_sizes[-1] + len(items))
                if self.dataset.all_chunk_engines:
                    self.dataset.item_added(items)
                return
            else:
                self.non_numpy_only()

        for item in items:
            self.append(item)
