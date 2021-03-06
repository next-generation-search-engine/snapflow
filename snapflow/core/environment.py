from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import asdict
from importlib import import_module
from typing import TYPE_CHECKING, Any, Iterator, List, Optional, Tuple, Union

import strictyaml
from loguru import logger
from snapflow.core.component import ComponentLibrary
from snapflow.core.metadata.orm import BaseModel
from snapflow.core.module import DEFAULT_LOCAL_MODULE, SnapflowModule
from snapflow.schema.base import GeneratedSchema, Schema, SchemaLike
from snapflow.storage.storage import DatabaseStorageClass, PythonStorageClass
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from snapflow.storage.storage import Storage
    from snapflow.core.pipe import Pipe
    from snapflow.core.node import Node, NodeLike
    from snapflow.core.execution import RunContext, ExecutionManager
    from snapflow.core.data_block import DataBlock
    from snapflow.core.graph import Graph, DeclaredGraph, DEFAULT_GRAPH

DEFAULT_METADATA_STORAGE_URL = "sqlite://"  # in-memory sqlite


class Environment:
    library: ComponentLibrary
    storages: List[Storage]
    metadata_storage: Storage

    def __init__(
        self,
        name: str = None,
        metadata_storage: Union["Storage", str] = None,
        add_default_python_runtime: bool = True,
        initial_modules: List[SnapflowModule] = None,  # Defaults to `core` module
    ):
        from snapflow.core.runtime import Runtime, LocalPythonRuntimeEngine
        from snapflow.storage.storage import Storage, new_local_python_storage
        from snapflow.modules import core

        self.name = name
        if metadata_storage is None:
            metadata_storage = DEFAULT_METADATA_STORAGE_URL
            logger.warning(
                f"No metadata storage specified, using default sqlite db `{DEFAULT_METADATA_STORAGE_URL}`"
            )
        if isinstance(metadata_storage, str):
            metadata_storage = Storage.from_url(metadata_storage)
        if metadata_storage is None:
            raise Exception("Must specify metadata_storage or allow default")
        self.metadata_storage = metadata_storage
        self.initialize_metadata_database()
        self._local_module = DEFAULT_LOCAL_MODULE
        self.library = ComponentLibrary()
        self.storages = []
        self.runtimes = []
        self._metadata_sessions: List[Session] = []
        # if add_default_python_runtime:
        #     self.runtimes.append(
        #         Runtime(
        #             url="python://local",
        #             runtime_engine=LocalPythonRuntimeEngine,
        #         )
        #     )
        if initial_modules is None:
            initial_modules = [core]
        for m in initial_modules:
            self.add_module(m)

        self._local_python_storage = new_local_python_storage()
        self.add_storage(self._local_python_storage)
        self.runtimes.append(Runtime.from_storage(self._local_python_storage))

    def initialize_metadata_database(self):
        if not issubclass(
            self.metadata_storage.storage_engine.storage_class, DatabaseStorageClass
        ):
            raise ValueError(
                f"metadata storage expected a database, got {self.metadata_storage}"
            )
        conn = self.metadata_storage.get_api().get_engine()
        BaseModel.metadata.create_all(conn)
        self.Session = sessionmaker(bind=conn)

    def _get_new_metadata_session(self) -> Session:
        sess = self.Session()
        self._metadata_sessions.append(sess)
        return sess

    def clean_up_db_sessions(self):
        from snapflow.storage.db.api import dispose_all

        self.close_all_sessions()
        dispose_all()

    def close_all_sessions(self):
        for s in self._metadata_sessions:
            s.close()
        self._metadata_sessions = []

    def get_default_local_python_storage(self) -> Storage:
        return self._local_python_storage

    def get_local_module(self) -> SnapflowModule:
        return self._local_module

    def get_module_order(self) -> List[str]:
        return self.library.module_lookup_names

    def get_schema(self, schema_like: SchemaLike, sess: Session) -> Schema:
        if isinstance(schema_like, Schema):
            return schema_like
        try:
            return self.library.get_schema(schema_like)
        except KeyError:
            schema = self.get_generated_schema(schema_like, sess=sess)
            if schema is None:
                raise KeyError(schema_like)
            return schema

    def add_schema(self, schema: Schema):
        self.library.add_schema(schema)

    def get_generated_schema(
        self, schema_like: SchemaLike, sess: Session
    ) -> Optional[Schema]:
        if isinstance(schema_like, str):
            key = schema_like
        elif isinstance(schema_like, Schema):
            key = schema_like.key
        else:
            raise TypeError(schema_like)
        got = sess.query(GeneratedSchema).get(key)
        if got is None:
            return None
        return got.as_schema()

    def add_new_generated_schema(self, schema: Schema, sess: Session):
        logger.debug(f"Adding new generated schema {schema}")
        if schema.key in self.library.schemas:
            # Already exists
            return
        got = GeneratedSchema(key=schema.key, definition=asdict(schema))
        sess.add(got)
        sess.flush([got])
        self.library.add_schema(schema)

    def all_schemas(self) -> List[Schema]:
        return self.library.all_schemas()

    def get_pipe(self, pipe_like: str) -> Pipe:
        return self.library.get_pipe(pipe_like)

    def add_pipe(self, pipe: Pipe):
        self.library.add_pipe(pipe)

    def all_pipes(self) -> List[Pipe]:
        return self.library.all_pipes()

    def add_module(self, *modules: SnapflowModule):
        for module in modules:
            self.library.add_module(module)

    @contextmanager
    def session_scope(self, **kwargs):
        session = self.Session(**kwargs)
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def get_default_storage(self) -> Storage:
        from snapflow.storage.storage import StorageClass

        if len(self.storages) == 1:
            return self.storages[0]
        for s in self.storages:
            if s.url == self.metadata_storage.url:
                continue
            if s.storage_engine.storage_class == PythonStorageClass:
                continue
            return s
        return self.storages[0]

    def get_run_context(
        self, graph: Graph, target_storage: Storage = None, **kwargs
    ) -> RunContext:
        from snapflow.core.execution import RunContext

        if target_storage is None:
            target_storage = self.get_default_storage()
        target_storage = self.add_storage(target_storage)
        if issubclass(target_storage.storage_engine.storage_class, PythonStorageClass):
            # TODO: handle multiple targets better
            logging.warning(
                "Using MEMORY storage -- results of execution will NOT "
                "be persisted. Add a database or file storage to persist results."
            )
        args = dict(
            graph=graph,
            env=self,
            # metadata_session=sess,
            runtimes=self.runtimes,
            storages=self.storages,
            target_storage=target_storage,
            local_python_storage=self.get_default_local_python_storage(),
        )
        args.update(**kwargs)
        return RunContext(**args)  # type: ignore

    @contextmanager
    def run(
        self, graph: Graph, target_storage: Storage = None, **kwargs
    ) -> Iterator[ExecutionManager]:
        from snapflow.core.execution import ExecutionManager

        # self.session.begin_nested()
        ec = self.get_run_context(graph, target_storage=target_storage, **kwargs)
        em = ExecutionManager(ec)
        logger.debug(f"executing on graph {graph.adjacency_list()}")
        try:
            yield em
            # self.session.commit()
            logger.debug("COMMITTED")
        except Exception as e:
            # self.session.rollback()
            logger.debug("ROLLED")
            raise e
        finally:
            # TODO:
            # self.validate_and_clean_data_blocks(delete_intermediate=True)
            pass
            # self.session.close()

    def _get_graph_and_node(
        self,
        node_like: Optional[NodeLike] = None,
        graph: Optional[Union[Graph, DeclaredGraph]] = None,
    ) -> Tuple[Optional[Node], Graph]:
        from snapflow.core.graph import DEFAULT_GRAPH, DeclaredGraph, Graph
        from snapflow.core.node import Node, DeclaredNode

        node = None
        if graph is None:
            if hasattr(node_like, "graph"):
                graph = node_like.graph
            else:
                graph = DEFAULT_GRAPH
        if isinstance(graph, DeclaredGraph):
            graph = graph.instantiate(self)
        if node_like is not None:
            node = node_like
            if isinstance(node, str):
                node = graph.get_node(node_like)
            if isinstance(node, DeclaredNode):
                node = node.instantiate(self, graph)
            assert isinstance(node, Node)
        assert isinstance(graph, Graph)
        return node, graph

    def produce(
        self,
        node_like: Optional[NodeLike] = None,
        graph: Union[Graph, DeclaredGraph] = None,
        to_exhaustion: bool = True,
        **execution_kwargs: Any,
    ) -> Optional[DataBlock]:
        node, graph = self._get_graph_and_node(node_like, graph)
        if node is not None:
            dependencies = graph.get_all_upstream_dependencies_in_execution_order(node)
        else:
            dependencies = graph.get_all_nodes_in_execution_order()
        output = None
        sess = self._get_new_metadata_session()  # hanging session
        with self.run(graph, **execution_kwargs) as em:
            for dep in dependencies:
                output = em.execute(
                    dep, to_exhaustion=to_exhaustion, output_session=sess
                )
        return output

    def run_node(
        self,
        node_like: Optional[NodeLike] = None,
        graph: Union[Graph, DeclaredGraph] = None,
        to_exhaustion: bool = True,
        **execution_kwargs: Any,
    ) -> Optional[DataBlock]:
        node, graph = self._get_graph_and_node(node_like, graph)
        assert node is not None

        logger.debug(f"Running: {node_like}")
        sess = self._get_new_metadata_session()  # hanging session
        with self.run(graph, **execution_kwargs) as em:
            return em.execute(node, to_exhaustion=to_exhaustion, output_session=sess)

    def run_graph(
        self,
        graph: Union[Graph, DeclaredGraph],
        to_exhaustion: bool = True,
        **execution_kwargs: Any,
    ):
        from snapflow.core.graph import DeclaredGraph

        if isinstance(graph, DeclaredGraph):
            graph = graph.instantiate(self)
        nodes = graph.get_all_nodes_in_execution_order()
        with self.run(graph, **execution_kwargs) as em:
            for node in nodes:
                em.execute(node, to_exhaustion=to_exhaustion)

    def latest_output(self, node: NodeLike) -> Optional[DataBlock]:
        sess = self._get_new_metadata_session()  # hanging session
        n, g = self._get_graph_and_node(node)
        ctx = self.get_run_context(g)
        return n.latest_output(ctx, sess)

    def add_storage(
        self, storage_like: Union[Storage, str], add_runtime: bool = True
    ) -> Storage:
        from snapflow.storage.storage import Storage

        if isinstance(storage_like, str):
            sr = Storage.from_url(storage_like)
        elif isinstance(storage_like, Storage):
            sr = storage_like
        else:
            raise TypeError
        for s in self.storages:
            if s.url == sr.url:
                return s
        self.storages.append(sr)
        if add_runtime:
            from snapflow.core.runtime import Runtime

            try:
                rt = Runtime.from_storage(sr)
                self.runtimes.append(rt)
            except ValueError:
                pass
        return sr

    # def validate_and_clean_data_blocks(
    #     self, delete_memory=True, delete_intermediate=False, force: bool = False
    # ):
    #     from snapflow.core.data_block import (
    #         DataBlockMetadata,
    #         StoredDataBlockMetadata,
    #     )

    #     if delete_memory:
    #         deleted = (
    #             self.session.query(StoredDataBlockMetadata)
    #             .filter(StoredDataBlockMetadata.storage_url.startswith("memory:"))
    #             .delete(False)
    #         )
    #         print(f"{deleted} Memory StoredDataBlocks deleted")

    #     for block in self.session.query(DataBlockMetadata).filter(
    #         ~DataBlockMetadata.stored_data_blocks.any()
    #     ):
    #         print(f"#{block.id} {block.nominal_schema_key} is orphaned! SAD")
    #     if delete_intermediate:
    #         # TODO: does no checking if they are unprocessed or not...
    #         if not force:
    #             d = input(
    #                 "Are you sure you want to delete ALL intermediate DataBlocks? There is no undoing this operation. y/N?"
    #             )
    #             if not d or d.lower()[0] != "y":
    #                 return
    #         # Delete blocks with no DataSet
    #         cnt = (
    #             self.session.query(DataBlockMetadata)
    #             .filter(~DataBlockMetadata.data_sets.any(),)
    #             .update({DataBlockMetadata.deleted: True}, synchronize_session=False)
    #         )
    #         print(f"{cnt} intermediate DataBlocks deleted")


# Shortcuts
def produce(
    *args,
    env: Optional[Environment] = None,
    modules: Optional[List[SnapflowModule]] = None,
    **kwargs: Any,
) -> Optional[DataBlock]:
    if env is None:
        env = Environment()
    if modules is not None:
        for module in modules:
            env.add_module(module)
    return env.produce(*args, **kwargs)


def run_node(
    *args,
    env: Optional[Environment] = None,
    modules: Optional[List[SnapflowModule]] = None,
    **kwargs: Any,
) -> Optional[DataBlock]:
    if env is None:
        env = Environment()
    if modules is not None:
        for module in modules:
            env.add_module(module)
    return env.run_node(*args, **kwargs)


def run_graph(
    *args,
    env: Optional[Environment] = None,
    modules: Optional[List[SnapflowModule]] = None,
    **kwargs: Any,
):
    if env is None:
        env = Environment()
    if modules is not None:
        for module in modules:
            env.add_module(module)
    return env.run_graph(*args, **kwargs)


# def load_environment_from_yaml(yml) -> Environment:
#     from snapflow.storage.storage import Storage

#     env = Environment(
#         metadata_storage=yml.get("metadata_storage", None),
#         add_default_python_runtime=yml.get("add_default_python_runtime", True),
#     )
#     for url in yml.get("storages", []):
#         env.add_storage(Storage.from_url(url))
#     for module_name in yml.get("modules", []):
#         m = import_module(module_name)
#         env.add_module(m)
#     return env


def load_environment_from_project(project: Any) -> Environment:
    from snapflow.storage.storage import Storage

    env = Environment(
        metadata_storage=getattr(project, "metadata_storage", None),
        add_default_python_runtime=getattr(project, "add_default_python_runtime", True),
    )
    for url in getattr(project, "storages", []):
        env.add_storage(Storage.from_url(url))
    for module_name in getattr(project, "modules", []):
        m = import_module(module_name)
        env.add_module(m)  # type: ignore  # We hijack the module
    return env


def current_env(cfg_module: str = None) -> Optional[Environment]:
    import sys
    from snapflow.project.project import SNAPFLOW_PROJECT_PACKAGE_NAME

    if cfg_module is None:
        cfg_module = SNAPFLOW_PROJECT_PACKAGE_NAME
    sys.path.append(os.getcwd())
    try:
        cfg = import_module(cfg_module)
        return load_environment_from_project(cfg)
    except ImportError:
        pass
    # with open(cfg_file) as f:
    #     yml = strictyaml.load(f.read()).data
    # return load_environment_from_yaml(yml)
    return None
