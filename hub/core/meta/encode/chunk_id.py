from hub.util.exceptions import ChunkIdEncoderError
import hub
from hub.core.storage.cachable import Cachable
from io import BytesIO
from typing import Optional, Tuple
import numpy as np
from uuid import uuid4


CHUNK_ID_INDEX = 0
LAST_INDEX_INDEX = 1


class ChunkIdEncoder(Cachable):
    def __init__(self):
        """Custom compressor that allows reading of chunk IDs from a sample index without decompressing.

        Chunk IDs:
            Chunk IDs are a uint32 value (64-bits) and this class handles generating/encoding them.

        Layout:
            `_encoded_ids` is a 2D array.

            Rows:
                The number of rows is equal to the number of chunk IDs this encoder is responsible for.

            Columns:
                The number of columns is 2.
                Each row looks like this: [chunk_id, last_index], where `last_index` is the last index that the
                chunk with `chunk_id` contains.

            Example:
                >>> enc = ChunkIdEncoder()
                >>> enc.generate_chunk_id()
                >>> enc.num_chunks
                1
                >>> enc.register_samples_to_last_chunk_id(10)
                >>> enc.num_samples
                10
                >>> enc.register_samples_to_last_chunk_id(10)
                >>> enc.num_samples
                20
                >>> enc.num_chunks
                1
                >>> enc.generate_chunk_id()
                >>> enc.register_samples_to_last_chunk_id(1)
                >>> enc.num_samples
                21
                >>> enc._encoded_ids
                [[3723322941, 19],
                 [1893450271, 20]]
                >>> enc[20]
                1893450271

            Best case scenario:
                The best case scenario is when all samples fit within a single chunk. This means the number of rows is 1,
                providing a O(1) lookup.

            Worst case scenario:
                The worst case scenario is when only 1 sample fits per chunk. This means the number of rows is equal to the number
                of samples, providing a O(log(N)) lookup.

            Lookup algorithm:
                To get the chunk ID for some sample index, you do a binary search over the right-most column. This will give you
                the row that corresponds to that sample index (since the right-most column is our "last index" for that chunk ID).
                Then, you get the left-most column and that is your chunk ID!

        """

        self._encoded_ids = None

    def tobytes(self) -> memoryview:
        bio = BytesIO()
        np.savez(
            bio,
            version=hub.__encoded_version__,
            ids=self._encoded_ids,
        )
        return bio.getbuffer()

    @staticmethod
    def name_from_id(id: np.uint64) -> str:
        return hex(id)[2:]

    @staticmethod
    def id_from_name(name: str) -> np.uint64:
        return int("0x" + name, 16)

    @classmethod
    def frombuffer(cls, buffer: bytes):
        instance = cls()
        bio = BytesIO(buffer)
        npz = np.load(bio)
        instance._encoded_ids = npz["ids"]
        return instance

    @property
    def num_chunks(self) -> int:
        if self._encoded_ids is None:
            return 0
        return len(self._encoded_ids)

    @property
    def num_samples(self) -> int:
        if self._encoded_ids is None:
            return 0
        return int(self._encoded_ids[-1, LAST_INDEX_INDEX] + 1)

    def generate_chunk_id(self) -> np.uint64:
        """Generates a random 64bit chunk ID using uuid4. Also prepares this ID to have samples registered to it.
        This method should be called once per chunk created.

        Returns:
            np.uint64: The random chunk ID.
        """

        id = np.uint64(uuid4().int >> 64)  # `id` is 64 bits after right shift

        if self.num_samples == 0:
            self._encoded_ids = np.array([[id, -1]], dtype=np.uint64)

        else:
            last_index = self.num_samples - 1

            new_entry = np.array(
                [[id, last_index]],
                dtype=np.uint64,
            )
            self._encoded_ids = np.concatenate([self._encoded_ids, new_entry])

        return id

    def register_samples_to_last_chunk_id(self, num_samples: int):
        """Registers samples to the chunk ID that was generated last with the `generate_chunk_id` method.
        This method should be called at least once per chunk created.

        Args:
            num_samples (int): The number of samples the last chunk ID should have added to it's registration.

        Raises:
            ValueError: `num_samples` should be non-negative.
            ChunkIdEncoderError: Must call `generate_chunk_id` before registering samples.
            ChunkIdEncoderError: `num_samples` can only be 0 if it is able to be a sample continuation accross chunks.
        """

        if num_samples < 0:
            raise ValueError(
                f"Cannot register negative num samples. Got: {num_samples}"
            )

        if self.num_samples == 0:
            raise ChunkIdEncoderError(
                "Cannot register samples because no chunk IDs exist."
            )

        if num_samples == 0 and self.num_chunks < 2:
            raise ChunkIdEncoderError(
                "Cannot register 0 num_samples (signifying a partial sample continuing the last chunk) when no last chunk exists."
            )

        current_entry = self._encoded_ids[-1]

        # this operation will trigger an overflow for the first addition, so supress the warning
        np.seterr(over="ignore")
        current_entry[LAST_INDEX_INDEX] += np.uint64(num_samples)
        np.seterr(over="warn")

    def get_name_for_chunk(self, idx) -> str:
        return ChunkIdEncoder.name_from_id(self._encoded_ids[:, CHUNK_ID_INDEX][idx])

    def get_local_sample_index(self, global_sample_index: int) -> int:
        # TODO: docstring

        _, chunk_index = self.__getitem__(global_sample_index, return_chunk_index=True)

        if global_sample_index < 0:
            raise NotImplementedError

        if chunk_index == 0:
            return global_sample_index

        current_entry = self._encoded_ids[chunk_index - 1]
        last_num_samples = current_entry[LAST_INDEX_INDEX] + 1

        return int(global_sample_index - last_num_samples)

    def __getitem__(
        self, sample_index: int, return_chunk_index: bool = False
    ) -> Tuple[Tuple[np.uint64], Optional[Tuple[int]]]:
        # TODO: docstring

        if self.num_samples == 0:
            raise IndexError(
                f"Index {sample_index} is out of bounds for an empty chunk names encoding."
            )

        if sample_index < 0:
            sample_index = (self.num_samples) + sample_index

        idx = np.searchsorted(self._encoded_ids[:, LAST_INDEX_INDEX], sample_index)
        id = self._encoded_ids[idx, CHUNK_ID_INDEX]
        chunk_index = idx

        if return_chunk_index:
            return id, chunk_index

        return id
