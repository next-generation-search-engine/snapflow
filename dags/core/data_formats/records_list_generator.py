from __future__ import annotations

from collections import abc
from typing import TYPE_CHECKING, Any, Optional, Type

from dags.core.data_formats.base import MemoryDataFormatBase, ReusableGenerator
from dags.core.data_formats.records_list import RecordsList

if TYPE_CHECKING:
    from dags import ObjectType


class RecordsListGenerator(ReusableGenerator[RecordsList]):
    pass


class RecordsListGeneratorFormat(MemoryDataFormatBase):
    @classmethod
    def type(cls) -> Type:
        return RecordsListGenerator

    @classmethod
    def maybe_instance(cls, obj: Any) -> bool:
        return isinstance(obj, abc.Generator)

    @classmethod
    def definitely_instance(cls, obj: Any) -> bool:
        return isinstance(obj, cls.type())

    @classmethod
    def infer_otype_from_records(cls, records: RecordsListGenerator) -> ObjectType:
        from dags.core.typing.inference import infer_otype_from_records_list
        from dags.core.data_formats import RecordsList, get_records_list_sample

        dl = get_records_list_sample(records)
        if dl is None:
            raise ValueError("Empty records object")
        inferred_otype = infer_otype_from_records_list(dl)
        return inferred_otype
