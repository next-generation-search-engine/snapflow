from __future__ import annotations

from dataclasses import dataclass

from pandas import DataFrame

from dags import DataSet
from dags.core.pipe import pipe
from dags.core.runnable import PipeContext
from dags.core.typing.object_type import ObjectTypeLike
from dags.utils.data import read_csv


@dataclass
class LocalResourceState:
    extracted: bool


@dataclass
class DataFrameResourceConfig:
    dataframe: DataFrame
    otype: ObjectTypeLike


@pipe(
    "core.extract_dataframe",
    config_class=DataFrameResourceConfig,
    state_class=LocalResourceState,
)
def extract_dataframe(ctx: PipeContext,) -> DataSet:
    extracted = ctx.get_state_value("extracted")
    if extracted:
        # Just emit once
        return
    ctx.emit_state_value("extracted", True)
    return ctx.get_config_value("dataframe")


@dataclass
class LocalCSVResourceConfig:
    path: str
    otype: ObjectTypeLike


@pipe(
    "core.extract_csv",
    config_class=LocalCSVResourceConfig,
    state_class=LocalResourceState,
)
def extract_csv(ctx: PipeContext,) -> DataSet:
    extracted = ctx.get_state_value("extracted")
    if extracted:
        # Static resource, if already emitted, return
        return
    path = ctx.get_config_value("path")
    with open(path) as f:
        records = read_csv(f.readlines())
    ctx.emit_state_value("extracted", True)
    return records
