from typing import Sequence

from pandas import DataFrame, concat

from dags.core.conversion.converter import ConversionCostLevel, Converter, StorageFormat
from dags.core.data_block import LocalMemoryDataRecords, StoredDataBlockMetadata
from dags.core.data_formats import (
    DataFormat,
    DataFrameFormat,
    DataFrameGenerator,
    DataFrameGeneratorFormat,
    RecordsList,
    RecordsListFormat,
    RecordsListGenerator,
    RecordsListGeneratorFormat,
)
from dags.core.storage.storage import LocalMemoryStorageEngine, StorageType
from dags.core.typing.object_type import ObjectType
from dags.utils.pandas import dataframe_to_records_list, records_list_to_dataframe


class MemoryToMemoryConverter(Converter):
    # TODO: we DON'T in general want to convert a GENERATOR to a non-GENERATOR prematurely.
    #   Currently, for example, if we want DataFrameGenerator to DBTable, the conversion path
    #   might be: DFG -> RecordsList -> DBTable. What we want is DFG -> RLG -> DBTable
    #   One solution is to add differing cost nuance
    # TODO: parameterized costs
    supported_input_formats: Sequence[StorageFormat] = (
        StorageFormat(StorageType.DICT_MEMORY, DataFrameFormat),
        StorageFormat(StorageType.DICT_MEMORY, RecordsListFormat),
        StorageFormat(StorageType.DICT_MEMORY, RecordsListGeneratorFormat),
        StorageFormat(StorageType.DICT_MEMORY, DataFrameGeneratorFormat),
    )
    supported_output_formats: Sequence[StorageFormat] = (
        StorageFormat(StorageType.DICT_MEMORY, RecordsListFormat),
        StorageFormat(StorageType.DICT_MEMORY, DataFrameFormat),
    )
    cost_level = ConversionCostLevel.MEMORY

    def _convert(
        self, input_sdb: StoredDataBlockMetadata, output_sdb: StoredDataBlockMetadata,
    ) -> StoredDataBlockMetadata:
        input_memory_storage = LocalMemoryStorageEngine(self.env, input_sdb.storage)
        output_memory_storage = LocalMemoryStorageEngine(self.env, output_sdb.storage)
        input_ldr = input_memory_storage.get_local_memory_data_records(input_sdb)
        lookup = {
            (DataFrameFormat, RecordsListFormat,): self.dataframe_to_records_list,
            (RecordsListFormat, DataFrameFormat,): self.records_list_to_dataframe,
            (
                RecordsListGeneratorFormat,
                DataFrameFormat,
            ): self.records_list_generator_to_dataframe,
            (
                RecordsListGeneratorFormat,
                RecordsListFormat,
            ): self.records_list_generator_to_records_list,
            (
                DataFrameGeneratorFormat,
                DataFrameFormat,
            ): self.dataframe_generator_to_dataframe,
            (
                DataFrameGeneratorFormat,
                RecordsListFormat,
            ): self.dataframe_generator_to_records_list,
        }
        try:
            output_records_object = lookup[
                (input_sdb.data_format, output_sdb.data_format)
            ](input_ldr.records_object, input_sdb.get_realized_otype(self.env))
        except KeyError:
            raise NotImplementedError((input_sdb.data_format, output_sdb.data_format))
        output_ldr = LocalMemoryDataRecords.from_records_object(output_records_object)
        output_memory_storage.store_local_memory_data_records(output_sdb, output_ldr)
        return output_sdb

    def dataframe_to_records_list(
        self, input_object: DataFrame, otype: ObjectType
    ) -> RecordsList:
        return dataframe_to_records_list(input_object, otype)

    def records_list_to_dataframe(
        self, input_object: RecordsList, otype: ObjectType
    ) -> DataFrame:
        return records_list_to_dataframe(input_object, otype)

    def records_list_generator_to_records_list(
        self, input_object: RecordsListGenerator, otype: ObjectType
    ) -> RecordsList:
        all_ = []
        for dl in input_object.get_generator():
            all_.extend(dl)
        return all_

    def records_list_generator_to_dataframe(
        self, input_object: RecordsListGenerator, otype: ObjectType
    ) -> DataFrame:
        return self.records_list_to_dataframe(
            self.records_list_generator_to_records_list(input_object, otype), otype
        )

    def dataframe_generator_to_records_list(
        self, input_object: DataFrameGenerator, otype: ObjectType
    ) -> RecordsList:
        return self.dataframe_to_records_list(
            self.dataframe_generator_to_dataframe(input_object, otype), otype
        )

    def dataframe_generator_to_dataframe(
        self, input_object: DataFrameGenerator, otype: ObjectType
    ) -> DataFrame:
        all_ = []
        for dl in input_object.get_generator():
            all_.append(dl)
        return concat(all_)
