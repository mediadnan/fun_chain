import asyncio
import functools
from abc import ABC, abstractmethod, ABCMeta
from enum import Enum, auto
from typing import (TypeVar,
                    TypeAlias,
                    Union,
                    Callable,
                    Coroutine,
                    Any,
                    Self,
                    Iterable)
from failures import Reporter

from ._tools import asyncify
from .name import validate as validate_name


T = TypeVar('T')
U = TypeVar('U')
Feedback: TypeAlias = tuple[bool, T]
SingleInputFunction: TypeAlias = Callable[[T], U]
SingleInputAsyncFunction: TypeAlias = Callable[[T], Coroutine[None, None, U]]


class Severity(Enum):
    OPTIONAL = auto()
    NORMAL = auto()
    REQUIRED = auto()


class Failed(Exception):
    """This error gets raised by a required node that failed; to stop the cascading execution"""


class BaseNode(ABC):
    """Base class for all FunChain nodes"""
    __slots__ = ('__severity',)
    __severity: Severity

    def __init__(self) -> None:
        self.severity = Severity.NORMAL

    def __or__(self, other: 'BaseNode') -> 'Chain' | 'AsyncChain':
        return Chain([self]) | other

    def __mul__(self, other: 'BaseNode') -> 'Chain' | 'AsyncChain':
        return Chain([self]) * other

    def __call__(self, arg, /, *, reporter: Reporter = None):
        """Processes arg and returns the result"""
        try:
            return self.process(arg, self._process_reporter(reporter))[1]
        except Failed:
            return

    @staticmethod
    def _process_reporter(reporter: Reporter = None) -> Union[Reporter, type[Reporter]]:
        """Prepares the reporter"""
        if reporter is None:
            return Reporter
        elif isinstance(reporter, Reporter):
            return reporter
        raise TypeError("reporter must be instance of failures.Reporter")

    @abstractmethod
    def process(self, arg, reporter: Union[Reporter, type[Reporter]]) -> Feedback: ...

    @abstractmethod
    def to_async(self) -> 'AsyncBaseNode':
        """Returns an async version of the current node"""

    @property
    def severity(self) -> Severity:
        """Gets the node severity"""
        return self.__severity

    @severity.setter
    def severity(self, severity: Severity) -> None:
        if not isinstance(severity, Severity):
            raise TypeError("severity must be either NORMAL, OPTIONAL or REQUIRED")
        self.__severity = severity

    def rn(self, name: str) -> Self:
        """Returns a clone of the current node with the new name"""
        return SemanticNode(self, name)


class AsyncBaseNode(BaseNode):
    def __or__(self, other: BaseNode | 'AsyncBaseNode') -> 'AsyncChain':
        return AsyncChain([self]) | other

    def __mul__(self, other: BaseNode | 'AsyncBaseNode') -> 'AsyncChain':
        return AsyncChain([self]) * other

    def to_async(self) -> Self:
        """Returns the current node"""
        return self

    def rn(self, name: str) -> Self:
        return AsyncSemanticNode(self, name)

    @abstractmethod
    async def process(self, arg, reporter: Reporter) -> Feedback: ...

    async def __call__(self, arg, /, *, reporter: Reporter = None):
        try:
            return (await self.process(arg, self._process_reporter(reporter)))[1]
        except Failed:
            return


class Node(BaseNode):
    __slots__ = ('__fun', '__name')
    __fun: SingleInputFunction
    __name: str

    def __init__(self, fun: SingleInputFunction, name: str = None) -> None:
        super().__init__()
        self.__fun = fun
        self.name = name

    @property
    def fun(self) -> SingleInputFunction:
        """Gets the internal function"""
        return self.__fun

    @property
    def name(self) -> str:
        """Gets the name of the leaf node (function)"""
        return self.__name

    @name.setter
    def name(self, name: str | None) -> None:
        if name is None:
            try:
                name = self.__fun.__name__
                if name == '<lambda>':
                    name = 'lambda'
            except AttributeError:
                name = type(self.__fun).__name__
        else:
            validate_name(name)
        self.__name = name

    def to_async(self) -> 'AsyncNode':
        return AsyncNode(asyncify(self.__fun), self.__name)

    def partial(self, *args, **kwargs) -> Self:
        """Clones the node and partially applies the arguments"""
        func = self.__fun
        while isinstance(func, functools.partial):
            args = *func.args, *args
            kwargs = {**func.keywords, **kwargs}
            func = func.func
        return self.__class__(functools.partial(func, *args, **kwargs), self.__name)

    def rn(self, name: str | None = None) -> Self:
        """
        Returns a clone of the current node with the new name,
        or a clone with the default function name if no name is passed
        """
        return self.__class__(self.__fun, name)

    def process(self, arg, reporter: Reporter) -> Feedback:
        try:
            return True, self.__fun(arg)
        except Exception as error:
            return self.handle_failure(error, arg, reporter)

    def handle_failure(self, error: Exception, arg, reporter: Reporter) -> Feedback:
        """Reports the failure according to the node severity"""
        severity = self.severity
        if severity is not Severity.OPTIONAL:
            reporter(self.name).report(error, input=arg)
        if severity is Severity.REQUIRED:
            raise Failed
        return False, None


class AsyncNode(Node, AsyncBaseNode):
    fun: SingleInputAsyncFunction

    async def process(self, arg, reporter: Reporter) -> Feedback:
        try:
            return True, await self.fun(arg)
        except Exception as error:
            return self.handle_failure(error, arg, reporter)


class WrapperNode(BaseNode, metaclass=ABCMeta):
    __slots__ = ('__node',)
    __node: BaseNode

    def __init__(self, node: BaseNode, /) -> None:
        super().__init__()
        self.__node = node

    @property
    def node(self) -> BaseNode:
        """Returns the wrapped node (Read-only)"""
        return self.__node

    @node.setter
    def node(self, node: BaseNode) -> None:
        if not isinstance(node, BaseNode):
            raise TypeError("node must be instance of failures.BaseNode")
        self.__node = node


class SemanticNode(WrapperNode):
    """This node holds the label for to be reported in case of failure"""
    __slots__ = ('__name',)
    __name: str

    def __init__(self, node: BaseNode, /, name: str) -> None:
        super().__init__(node)
        self.name = name

    def process(self, arg, reporter: Reporter) -> Feedback:
        return self.node.process(arg, reporter(self.name))

    @property
    def name(self) -> str:
        """Gets the label of the node (Read-only)"""
        return self.__name

    @name.setter
    def name(self, name: str) -> None:
        validate_name(name)
        self.__name = name

    def rn(self, name: str | None = None) -> Self:
        return self.__class__(self.node, name) if name is not None else self.node

    def to_async(self) -> 'AsyncSemanticNode':
        return AsyncSemanticNode(self.node.to_async(), self.name)


class AsyncSemanticNode(SemanticNode, AsyncBaseNode):
    node: AsyncBaseNode

    async def process(self, arg, reporter: Reporter) -> Feedback:
        return await self.node.process(arg, reporter(self.name))


class Chain(BaseNode):
    __slots__ = '__nodes',
    __nodes: list[BaseNode]

    def __init__(self, nodes: Iterable[BaseNode], /) -> None:
        super().__init__()
        self.__nodes = list(nodes)

    def __or__(self, other: BaseNode) -> 'Chain':
        if not isinstance(other, BaseNode):
            raise TypeError("Chained node must be instance of failures.BaseNode")
        if isinstance(other, AsyncBaseNode):
            return self.to_async() | other
        return Chain([*self.__nodes, other])

    def __mul__(self, other: BaseNode) -> 'Chain':
        if not isinstance(other, BaseNode):
            raise TypeError("Chained node must be instance of failures.BaseNode")
        if isinstance(other, AsyncBaseNode):
            return self.to_async() * other
        return Chain([*self.__nodes, Loop(other)])

    @property
    def nodes(self) -> list[BaseNode]:
        """Gets a copy of the nodes (Read-only)"""
        return self.__nodes.copy()

    def to_async(self) -> 'AsyncChain':
        return AsyncChain([node.to_async() for node in self.nodes])

    def process(self, arg, reporter: Reporter) -> Feedback:
        for node in self.nodes:
            success, res = node.process(arg, reporter)
            if not success:
                if node.severity is Severity.OPTIONAL:
                    continue
                return False, None
            arg = res
        return True, arg


class AsyncChain(Chain, AsyncBaseNode):
    nodes: list[AsyncBaseNode]

    def __or__(self, other: BaseNode | AsyncBaseNode) -> 'AsyncChain':
        return AsyncChain([*self.nodes, other])

    def __mul__(self, other) -> 'AsyncChain':
        return AsyncChain([*self.nodes, AsyncLoop(other)])

    async def process(self, arg, reporter: Reporter) -> Feedback:
        for node in self.nodes:
            success, res = await node.process(arg, reporter)
            if not success:
                if node.severity is Severity.OPTIONAL:
                    continue
                return False, None
            arg = res
        return True, arg


class Loop(WrapperNode):
    """Wrapper node that processes each element of the input through the wrapped node and returns a list of results"""
    def to_async(self) -> 'AsyncLoop':
        return AsyncLoop(self.node.to_async())

    def process(self, args: Iterable, reporter: Reporter) -> Feedback:
        node = self.node
        successes: set[bool] = set()
        results = []
        for arg in args:
            success, res = node.process(arg, reporter)
            successes.add(success)
            results.append(res)
        return any(successes), results


class AsyncLoop(Loop, AsyncBaseNode):
    """A node that processes each element of the input asynchronously through the wrapped node and returns a list of results"""
    node: AsyncBaseNode

    async def process(self, args: Iterable, reporter: Reporter) -> Feedback:
        node = self.node
        jobs = await asyncio.gather(*[asyncio.create_task(node.process(arg, reporter)) for arg in args])
        successes, results = zip(*jobs)
        return any(successes), results


class Group(BaseNode):
    """A node that processes the input through multiple branches and returns a collection type as result"""
    __slots__ = ('__nodes',)
    __nodes: list[tuple[str, BaseNode]]

    def __init__(self, nodes: Iterable[tuple[str, BaseNode]], /):
        super().__init__()
        self.__nodes = list(nodes)

    @staticmethod
    @abstractmethod
    def convert(results: Iterable[tuple[Any, Any]]) -> Any:
        """Converts the branched results to a specific collection type"""

    def to_async(self) -> 'AsyncGroup':
        return AsyncGroup([(branch, node.to_async()) for branch, node in self.__nodes])

    @property
    def nodes(self) -> list[tuple[str, BaseNode]]:
        """Returns the node list (mutable)"""
        return self.__nodes

    def process(self, arg, reporter: Reporter) -> Feedback:
        successes: set[bool] = set()
        results: list[tuple[str, Any]] = []
        for name, node in self.__nodes:
            success, result = node.process(arg, reporter)
            if not success:
                if node.severity is Severity.OPTIONAL:
                    continue
                if node.severity is Severity.REQUIRED:
                    raise Failed
            successes.add(success)
            results.append((name, result))
        return any(successes), self.convert(results)


class AsyncGroup(Group, AsyncBaseNode, metaclass=ABCMeta):
    """A node that processes asynchronously the input through multiple branches and returns a collection type as result"""
    nodes: list[tuple[str, AsyncBaseNode]]

    async def process(self, arg, reporter: Reporter) -> Feedback:
        names, severities, tasks = zip(
            *((name, node.severity, asyncio.create_task(node.process(arg, reporter))) for name, node in self.nodes)
        )
        successes, results = zip(*(await asyncio.gather(*tasks)))
        for name, success, severity, result in zip(names, successes, severities, results):
            if not success:
                if severity is Severity.OPTIONAL:
                    continue
                if severity is Severity.REQUIRED:
                    raise Failed
            successes.add(success)
            results.append((name, result))
        return any(successes), self.convert(results)


def _dict_converter(results: Iterable[tuple[str, Any]]) -> dict:
    return {branch: result for branch, result in results}


def _list_converter(results: Iterable[tuple[str, Any]]) -> list:
    return [result for _, result in results]


class ListGroup(Group):
    """A node that processes the input through multiple branches and returns a list as result"""
    convert = staticmethod(_list_converter)


class AsyncListGroup(AsyncGroup):
    """A node that processes asynchronously the input through multiple branches and returns a list as result"""
    convert = staticmethod(_list_converter)


class DictGroup(Group):
    """A node that processes the input through multiple branches and returns a dictionary as result"""
    convert = staticmethod(_dict_converter)


class AsyncDictGroup(AsyncGroup):
    """A node that processes asynchronously the input through multiple branches and returns a dictionary as result"""
    convert = staticmethod(_dict_converter)
