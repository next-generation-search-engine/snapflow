from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import asdict
from importlib import import_module
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Union

from sqlalchemy.orm import Session, close_all_sessions, sessionmaker

from dags.core.component import ComponentLibrary
from dags.core.metadata.orm import BaseModel
from dags.core.module import DEFAULT_LOCAL_MODULE, DagsModule
from dags.core.typing.object_type import GeneratedObjectType, ObjectType, ObjectTypeLike
from dags.logging.event import Event, EventHandler, EventSubject, event_factory
from loguru import logger

if TYPE_CHECKING:
    from dags.core.streams import PipeNodeRawInput, DataBlockStream
    from dags.core.storage.storage import (
        Storage,
        new_local_memory_storage,
        StorageClass,
    )
    from dags.core.pipe import (
        PipeLike,
        Pipe,
    )
    from dags.core.node import Node
    from dags.core.runnable import ExecutionContext
    from dags.core.data_block import DataBlock
    from dags.core.graph import Graph


class Environment:
    library: ComponentLibrary
    storages: List[Storage]
    metadata_storage: Storage
    event_handlers: List[EventHandler]

    def __init__(
        self,
        name: str = None,
        metadata_storage: Union["Storage", str] = None,
        create_metadata_storage: bool = True,
        add_default_python_runtime: bool = True,
        initial_modules: List[DagsModule] = None,  # Defaults to `core` module
        event_handlers: List[EventHandler] = None,
    ):
        from dags.core.runtime import Runtime
        from dags.core.runtime import RuntimeClass
        from dags.core.runtime import RuntimeEngine
        from dags.core.storage.storage import Storage, new_local_memory_storage
        from dags.modules import core
        from dags.core.graph import Graph

        self.name = name
        if metadata_storage is None and create_metadata_storage:
            # TODO: kind of hidden. make configurable at least, and log/print to user
            metadata_storage = Storage.from_url("sqlite:///.dags_metadata.db")
            logger.warning(
                f"No metadata storage specified, using local '.dags_metadata.db' sqlite db"
            )
        if isinstance(metadata_storage, str):
            metadata_storage = Storage.from_url(metadata_storage)
        if metadata_storage is None:
            raise Exception("Must specify metadata_storage or allow default")
        self.metadata_storage = metadata_storage
        self.initialize_metadata_database()
        self._local_module = DEFAULT_LOCAL_MODULE  #     DagsModule(name=f"_env")
        self.library = ComponentLibrary()
        self.storages = []
        self.runtimes = []
        self._metadata_sessions: List[Session] = []
        if add_default_python_runtime:
            self.runtimes.append(
                Runtime(
                    url="python://local",
                    runtime_class=RuntimeClass.PYTHON,
                    runtime_engine=RuntimeEngine.LOCAL,
                )
            )
        if initial_modules is None:
            initial_modules = [core]
        for m in initial_modules:
            self.add_module(m)

        self.event_handlers = event_handlers or []
        self._local_memory_storage = new_local_memory_storage()
        self.add_storage(self._local_memory_storage)

    def initialize_metadata_database(self):
        from dags.core.metadata.listeners import add_persisting_sdb_listener
        from dags.core.storage.storage import StorageClass

        if self.metadata_storage.storage_class != StorageClass.DATABASE:
            raise ValueError(
                f"metadata storage expected a database, got {self.metadata_storage}"
            )
        conn = self.metadata_storage.get_database_api(self).get_engine()
        BaseModel.metadata.create_all(conn)
        self.Session = sessionmaker(bind=conn)
        add_persisting_sdb_listener(self.Session)

    def get_new_metadata_session(self) -> Session:
        sess = self.Session()
        self._metadata_sessions.append(sess)
        return sess

    def clean_up_db_sessions(self):
        from dags.db.api import dispose_all

        close_all_sessions()
        dispose_all()

    def close_sessions(self):
        for s in self._metadata_sessions:
            s.close()
        self._metadata_sessions = []

    def get_default_local_memory_storage(self) -> Storage:
        return self._local_memory_storage

    def get_local_module(self) -> DagsModule:
        return self._local_module

    def get_module_order(self) -> List[str]:
        return self.library.module_lookup_keys

    def get_otype(self, otype_like: ObjectTypeLike) -> ObjectType:
        if isinstance(otype_like, ObjectType):
            return otype_like
        try:
            return self.library.get_otype(otype_like)
        except KeyError:
            otype = self.get_generated_otype(otype_like)
            if otype is None:
                raise KeyError(otype_like)
            return otype

    def get_generated_otype(self, otype_like: ObjectTypeLike) -> Optional[ObjectType]:
        if isinstance(otype_like, str):
            key = otype_like
        elif isinstance(otype_like, ObjectType):
            key = otype_like.key
        else:
            raise TypeError(otype_like)
        with self.session_scope() as sess:
            got = sess.query(GeneratedObjectType).get(key)
            if got is None:
                return None
            return got.as_otype()

    def add_new_otype(self, otype: ObjectType):
        print(f"adding otype {otype.key}")
        if otype.key in self.library.otypes:
            # Already exists
            return
        got = GeneratedObjectType(key=otype.key, definition=asdict(otype))
        with self.session_scope() as sess:
            sess.add(got)
        self.library.add_otype(otype)

    def all_otypes(self) -> List[ObjectType]:
        return self.library.all_otypes()

    def get_pipe(self, pipe_like: str) -> Pipe:
        return self.library.get_pipe(pipe_like)

    def add_pipe(self, pipe: Pipe):
        self.library.add_pipe(pipe)

    def all_pipes(self) -> List[Pipe]:
        return self.library.all_pipes()

    # def add_node(self, key: str, pipe: Union[PipeLike, str], **kwargs: Any) -> Node:
    #     from dags.core.node import Node
    #
    #     if isinstance(pipe, str):
    #         pipe = self.get_pipe(pipe)
    #     node = Node(self, key, pipe, **kwargs)
    #     self._declared_graph.add_node(node)
    #     return node

    # def add_dataset_node(
    #     self,
    #     key: str,
    #     dataset_name: str = None,
    #     upstream: Any = None,
    #     **stream_kwargs: Any,
    # ) -> Node:
    #     from dags.core.streams import DataBlockStream
    #
    #     if dataset_name is None:
    #         dataset_name = name
    #     if upstream is None:
    #         upstream = DataBlockStream(**stream_kwargs)
    #     return self.add_node(
    #         name,
    #         "core.accumulate_as_dataset",
    #         config={"dataset_name": dataset_name},
    #         upstream=upstream,
    #     )

    # def all_added_nodes(self) -> List[Node]:
    #     return list(self._declared_graph.nodes())
    #
    # def all_flattened_nodes(self) -> List[Node]:
    #     # TODO: cache?
    #     return list(self._flattened_graph().nodes())
    #
    # def _flattened_graph(self) -> Graph:
    #     return self._declared_graph.with_dataset_nodes().flatten()
    #
    # def get_graph(self) -> Graph:
    #     return self._declared_graph

    # def get_node(self, node_like: Union[Node, str]) -> Node:
    #     from dags.core.node import Node
    #
    #     if isinstance(node_like, Node):
    #         return node_like
    #     # try:
    #     print(self._declared_graph)
    #     # print(self._declared_graph.as_networkx_graph(False))
    #     # print(self._declared_graph.as_networkx_graph(True))
    #     print("***")
    #     print(node_like)
    #     return self._declared_graph.get_node(node_like)
    #     # except KeyError:  # TODO: do we want to get flattened (sub) nodes too? Probably
    #     # return self._flattened_graph().get_node(node_like)

    # def get_pipe_graph_resolver(self) -> PipeGraphResolver:
    #     from dags.core.pipe_interface import PipeGraphResolver
    #
    #     return PipeGraphResolver(self)
    #
    # def set_upstream(self, node_like: Union[Node, str], upstream: PipeNodeRawInput):
    #     n = self.get_node(node_like)
    #     n.set_inputs(upstream)
    #
    # def add_upstream(self, node_like: Union[Node, str], upstream: PipeNodeRawInput):
    #     n = self.get_node(node_like)
    #     n.add_upstream(upstream)

    def add_module(self, module: DagsModule):
        self.library.add_module(module)

    # def get_indexable_components(self) -> Iterable[IndexableComponent]:
    #     for module in self.module_registry.all():
    #         for c in module.get_indexable_components():
    #             yield c

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

    def get_execution_context(
        self, graph: Graph, session: Session, target_storage: Storage = None, **kwargs
    ) -> ExecutionContext:
        from dags.core.runnable import ExecutionContext

        if target_storage is None:
            target_storage = self.storages[0] if self.storages else None
        args = dict(
            graph=graph,
            env=self,
            metadata_session=session,
            runtimes=self.runtimes,
            storages=self.storages,
            target_storage=target_storage,
            local_memory_storage=self.get_default_local_memory_storage(),
        )
        args.update(**kwargs)
        return ExecutionContext(**args)

    @contextmanager
    def execution(self, graph: Graph, target_storage: Storage = None, **kwargs):
        # TODO: target storage??
        from dags.core.runnable import ExecutionManager

        session = self.Session()
        ec = self.get_execution_context(
            graph, session, target_storage=target_storage, **kwargs
        )
        em = ExecutionManager(ec)
        try:
            yield em
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            # TODO:
            # self.validate_and_clean_data_blocks(delete_intermediate=True)
            session.close()

    def produce(
        self,
        graph: Graph,
        node_like: Union[Node, str],
        to_exhaustion: bool = True,
        **execution_kwargs: Any,
    ) -> Optional[DataBlock]:
        from dags.core.node import Node

        node = node_like
        if isinstance(node, str):
            node = graph.get_any_node(node_like)
        assert isinstance(node, Node)
        dependencies = graph.get_flattened_graph().get_all_upstream_dependencies_in_execution_order(
            node
        )
        output = None
        for dep in dependencies:
            with self.execution(graph, **execution_kwargs) as em:
                output = em.run(dep, to_exhaustion=to_exhaustion)
        return output

    def run_node(
        self,
        graph: Graph,
        node_like: Union[Node, str],
        to_exhaustion: bool = True,
        **execution_kwargs: Any,
    ) -> Optional[DataBlock]:
        from dags.core.node import Node

        if isinstance(node_like, str):
            node_like = graph.get_any_node(node_like)
        assert isinstance(node_like, Node)

        flattened_node = graph.get_flattened_graph().get_flattened_root_node_for_declared_node(
            node_like
        )
        logger.debug(f"Running: flattened node: {flattened_node} (from {node_like})")
        with self.execution(graph, **execution_kwargs) as em:
            return em.run(flattened_node, to_exhaustion=to_exhaustion)

    def run_graph(self, graph, to_exhaustion: bool = True, **execution_kwargs: Any):
        nodes = graph.get_flattened_graph().get_all_nodes_in_execution_order()
        for node in nodes:
            with self.execution(graph, **execution_kwargs) as em:
                em.run(node, to_exhaustion=to_exhaustion)

    # def get_latest_output(self, node: Node) -> Optional[DataBlock]:
    #     session = self.get_new_metadata_session()  # TODO: hanging session
    #     ctx = self.get_execution_context(session)
    #     return node.get_latest_output(ctx)

    def add_storage(
        self, storage_like: Union[Storage, str], add_runtime=True
    ) -> Storage:
        from dags.core.storage.storage import Storage

        if isinstance(storage_like, str):
            sr = Storage.from_url(storage_like)
        elif isinstance(storage_like, Storage):
            sr = storage_like
        else:
            raise TypeError
        self.storages.append(sr)
        if add_runtime:
            from dags.core.runtime import Runtime

            try:
                rt = Runtime.from_storage(sr)
                self.runtimes.append(rt)
            except ValueError:
                pass
        return sr

    def validate_and_clean_data_blocks(
        self, delete_memory=True, delete_intermediate=False, force: bool = False
    ):
        with self.session_scope() as sess:
            from dags.core.data_block import (
                DataBlockMetadata,
                StoredDataBlockMetadata,
            )

            if delete_memory:
                deleted = (
                    sess.query(StoredDataBlockMetadata)
                    .filter(StoredDataBlockMetadata.storage_url.startswith("memory:"))
                    .delete(False)
                )
                print(f"{deleted} Memory StoredDataBlocks deleted")

            for block in sess.query(DataBlockMetadata).filter(
                ~DataBlockMetadata.stored_data_blocks.any()
            ):
                print(f"#{block.id} {block.expected_otype_key} is orphaned! SAD")
            if delete_intermediate:
                # TODO: does no checking if they are unprocessed or not...
                if not force:
                    d = input(
                        "Are you sure you want to delete ALL intermediate DataBlocks? There is no undoing this operation. y/N?"
                    )
                    if not d or d.lower()[0] != "y":
                        return
                # Delete DRs with no DataSet
                cnt = (
                    sess.query(DataBlockMetadata)
                    .filter(~DataBlockMetadata.data_sets.any(),)
                    .update(
                        {DataBlockMetadata.deleted: True}, synchronize_session=False
                    )
                )
                print(f"{cnt} intermediate DataBlocks deleted")

    def add_event_handler(self, eh: EventHandler):
        self.event_handlers.append(eh)

    def send_event(
        self, event_subject: Union[EventSubject, str], event_details, **kwargs
    ) -> Event:
        e = event_factory(event_subject, event_details, **kwargs)
        for handler in self.event_handlers:
            handler.handle(e)
        return e


# Not supporting yml project config atm
# def load_environment_from_yaml(yml) -> Environment:
#     from dags.core.storage.storage import StorageResource
#
#     env = Environment(
#         metadata_storage=yml.get("metadata_storage", None),
#         add_default_python_runtime=yml.get("add_default_python_runtime", True),
#     )
#     for url in yml.get("storages"):
#         env.add_storage(StorageResource.from_url(url))
#     for module_key in yml.get("module_lookup_keys"):
#         m = import_module(module_key)
#         env.add_module(m)
#     return env


def load_environment_from_project(project: Any) -> Environment:
    from dags.core.storage.storage import Storage

    env = Environment(
        metadata_storage=getattr(project, "metadata_storage", None),
        add_default_python_runtime=getattr(project, "add_default_python_runtime", True),
    )
    for url in getattr(project, "storages", []):
        env.add_storage(Storage.from_url(url))
    for module_key in getattr(project, "module_lookup_keys", []):
        m = import_module(module_key)
        env.add_module(m)  # type: ignore  # We hijack the module
    return env


def current_env(cfg_module: str = "project") -> Environment:
    import sys

    sys.path.append(os.getcwd())
    cfg = import_module(cfg_module)
    return load_environment_from_project(cfg)
