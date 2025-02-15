# coding=utf-8
#
# This file is part of Hypothesis, which may be found at
# https://github.com/HypothesisWorks/hypothesis/
#
# Most of this work is copyright (C) 2013-2019 David R. MacIver
# (david@drmaciver.com), but it contains contributions by others. See
# CONTRIBUTING.rst for a full list of people who may hold copyright, and
# consult the git log if you need to determine who owns an individual
# contribution.
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at https://mozilla.org/MPL/2.0/.
#
# END HEADER

from __future__ import absolute_import, division, print_function

from collections import defaultdict
from enum import IntEnum

import attr

from hypothesis.errors import Frozen, InvalidArgument, StopTest
from hypothesis.internal.compat import (
    benchmark_time,
    bit_length,
    hbytes,
    hrange,
    int_from_bytes,
    int_to_bytes,
    text_type,
    unicode_safe_repr,
)
from hypothesis.internal.conjecture.junkdrawer import IntList
from hypothesis.internal.conjecture.utils import calc_label_from_name
from hypothesis.internal.escalation import mark_for_escalation

TOP_LABEL = calc_label_from_name("top")
DRAW_BYTES_LABEL = calc_label_from_name("draw_bytes() in ConjectureData")


class ExtraInformation(object):
    """A class for holding shared state on a ``ConjectureData`` that should
    be added to the final ``ConjectureResult``."""

    def __repr__(self):
        return "ExtraInformation(%s)" % (
            ", ".join(["%s=%r" % (k, v) for k, v in self.__dict__.items()]),
        )

    def has_information(self):
        return bool(self.__dict__)


class Status(IntEnum):
    OVERRUN = 0
    INVALID = 1
    VALID = 2
    INTERESTING = 3

    def __repr__(self):
        return "Status.%s" % (self.name,)


class Example(object):
    """Examples track the hierarchical structure of draws from the byte stream,
    within a single test run.

    Examples are created to mark regions of the byte stream that might be
    useful to the shrinker, such as:
    - The bytes used by a single draw from a strategy.
    - Useful groupings within a strategy, such as individual list elements.
    - Strategy-like helper functions that aren't first-class strategies.
    - Each lowest-level draw of bits or bytes from the byte stream.
    - A single top-level example that spans the entire input.

    Example-tracking allows the shrinker to try "high-level" transformations,
    such as rearranging or deleting the elements of a list, without having
    to understand their exact representation in the byte stream.

    Rather than store each ``Example`` as a rich object, it is actually
    just an index into the ``Examples`` class defined below. This has two
    purposes: Firstly, for most properties of examples we will never need
    to allocate storage at all, because most properties are not used on
    most examples. Secondly, by storing the properties as compact lists
    of integers, we save a considerable amount of space compared to
    Python's normal object size.

    This does have the downside that it increases the amount of allocation
    we do, and slows things down as a result, in some usage patterns because
    we repeatedly allocate the same Example or int objects, but it will
    often dramatically reduce our memory usage, so is worth it.
    """

    __slots__ = ("owner", "index")

    def __init__(self, owner, index):
        self.owner = owner
        self.index = index

    def __eq__(self, other):
        if self is other:
            return True
        if not isinstance(other, Example):
            return NotImplemented
        return (self.owner is other.owner) and (self.index == other.index)

    def __ne__(self, other):
        if self is other:
            return False
        if not isinstance(other, Example):
            return NotImplemented
        return (self.owner is not other.owner) or (self.index != other.index)

    def __repr__(self):
        return "examples[%d]" % (self.index,)

    @property
    def label(self):
        """A label is an opaque value that associates each example with its
        approximate origin, such as a particular strategy class or a particular
        kind of draw."""
        return self.owner.labels[self.owner.label_indices[self.index]]

    @property
    def parent(self):
        """The index of the example that this one is nested directly within."""
        if self.index == 0:
            return None
        return self.owner.parentage[self.index]

    @property
    def start(self):
        """The position of the start of this example in the byte stream."""
        return self.owner.starts[self.index]

    @property
    def end(self):
        """The position directly after the last byte in this byte stream.
        i.e. the example corresponds to the half open region [start, end).
        """
        return self.owner.ends[self.index]

    @property
    def depth(self):
        """Depth of this example in the example tree. The top-level example has a
        depth of 0."""
        return self.owner.depths[self.index]

    @property
    def trivial(self):
        """An example is "trivial" if it only contains forced bytes and zero bytes.
        All examples start out as trivial, and then get marked non-trivial when
        we see a byte that is neither forced nor zero."""
        return self.index in self.owner.trivial

    @property
    def discarded(self):
        """True if this is example's ``stop_example`` call had ``discard`` set to
        ``True``. This means we believe that the shrinker should be able to delete
        this example completely, without affecting the value produced by its enclosing
        strategy. Typically set when a rejection sampler decides to reject a
        generated value and try again."""
        return self.index in self.owner.discarded

    @property
    def length(self):
        """The number of bytes in this example."""
        return self.end - self.start

    @property
    def children(self):
        """The list of all examples with this as a parent, in increasing index
        order."""
        return [self.owner[i] for i in self.owner.children[self.index]]


class ExampleProperty(object):
    """There are many properties of examples that we calculate by
    essentially rerunning thet test case multiple times based on the
    calls which we record in ExampleRecord.

    This class defines a visitor, subclasses of which can be used
    to calculate these properties.
    """

    def __init__(self, examples):
        self.example_stack = []
        self.examples = examples
        self.bytes_read = 0
        self.example_count = 0
        self.block_count = 0

    def run(self):
        """Rerun the test case with this visitor and return the
        results of ``self.finish()``."""
        self.begin()
        blocks = self.examples.blocks
        for record in self.examples.trail:
            if record == DRAW_BITS_RECORD:
                self.__push(0)
                self.bytes_read = blocks.endpoints[self.block_count]
                self.block(self.block_count)
                self.block_count += 1
                self.__pop(False)
            elif record >= START_EXAMPLE_RECORD:
                self.__push(record - START_EXAMPLE_RECORD)
            else:
                assert record in (
                    STOP_EXAMPLE_DISCARD_RECORD,
                    STOP_EXAMPLE_NO_DISCARD_RECORD,
                )
                self.__pop(record == STOP_EXAMPLE_DISCARD_RECORD)
        return self.finish()

    def __push(self, label_index):
        i = self.example_count
        assert i < len(self.examples)
        self.start_example(i, label_index)
        self.example_count += 1
        self.example_stack.append(i)

    def __pop(self, discarded):
        i = self.example_stack.pop()
        self.stop_example(i, discarded)

    def begin(self):
        """Called at the beginning of the run to initialise any
        relevant state."""
        self.result = IntList.of_length(len(self.examples))

    def start_example(self, i, label_index):
        """Called at the start of each example, with ``i`` the
        index of the example and ``label_index`` the index of
        its label in ``self.examples.labels``."""

    def block(self, i):
        """Called with each ``draw_bits`` call, with ``i`` the index of the
        corresonding block in ``self.examples.blocks``"""

    def stop_example(self, i, discarded):
        """Called at the end of each example, with ``i`` the
        index of the example and ``discarded`` being ``True`` if ``stop_example``
        was called with ``discard=True``."""

    def finish(self):
        return self.result


def calculated_example_property(cls):
    """Given an ``ExampleProperty`` as above we use this decorator
    to transform it into a lazy property on the ``Examples`` class,
    which has as its value the result of calling ``cls.run()``,
    computed the first time the property is accessed.

    This has the slightly weird result that we are defining nested
    classes which get turned into properties."""
    name = cls.__name__
    cache_name = "__" + name

    def lazy_calculate(self):
        result = getattr(self, cache_name, None)
        if result is None:
            result = cls(self).run()
            setattr(self, cache_name, result)
        return result

    lazy_calculate.__name__ = cls.__name__
    lazy_calculate.__qualname__ = getattr(cls, "__qualname__", cls.__name__)
    return property(lazy_calculate)


DRAW_BITS_RECORD = 0
STOP_EXAMPLE_DISCARD_RECORD = 1
STOP_EXAMPLE_NO_DISCARD_RECORD = 2
START_EXAMPLE_RECORD = 3


class ExampleRecord(object):
    """Records the series of ``start_example``, ``stop_example``, and
    ``draw_bits`` calls so that these may be stored in ``Examples`` and
    replayed when we need to know about the structure of individual
    ``Example`` objects.

    Note that there is significant similarity between this class and
    ``DataObserver``, and the plan is to eventually unify them, but
    they currently have slightly different functions and implementations.
    """

    def __init__(self):
        self.labels = [DRAW_BYTES_LABEL]
        self.__index_of_labels = {DRAW_BYTES_LABEL: 0}
        self.trail = IntList()

    def freeze(self):
        self.__index_of_labels = None

    def start_example(self, label):
        try:
            i = self.__index_of_labels[label]
        except KeyError:
            i = self.__index_of_labels.setdefault(label, len(self.labels))
            self.labels.append(label)
        self.trail.append(START_EXAMPLE_RECORD + i)

    def stop_example(self, discard):
        if discard:
            self.trail.append(STOP_EXAMPLE_DISCARD_RECORD)
        else:
            self.trail.append(STOP_EXAMPLE_NO_DISCARD_RECORD)

    def draw_bits(self, n, forced):
        self.trail.append(DRAW_BITS_RECORD)


class Examples(object):
    """A lazy collection of ``Example`` objects, derived from
    the record of recorded behaviour in ``ExampleRecord``.

    Behaves logically as if it were a list of ``Example`` objects,
    but actually mostly exists as a compact store of information
    for them to reference into. All properties on here are best
    understood as the backing storage for ``Example`` and are
    described there.
    """

    def __init__(self, record, blocks):
        self.trail = record.trail
        self.labels = record.labels
        self.__length = (
            self.trail.count(STOP_EXAMPLE_DISCARD_RECORD)
            + record.trail.count(STOP_EXAMPLE_NO_DISCARD_RECORD)
            + record.trail.count(DRAW_BITS_RECORD)
        )
        self.__example_lengths = None

        self.blocks = blocks
        self.__children = None

    @calculated_example_property
    class starts_and_ends(ExampleProperty):
        def begin(self):
            self.starts = IntList.of_length(len(self.examples))
            self.ends = IntList.of_length(len(self.examples))

        def start_example(self, i, label_index):
            self.starts[i] = self.bytes_read

        def stop_example(self, i, label_index):
            self.ends[i] = self.bytes_read

        def finish(self):
            return (self.starts, self.ends)

    @property
    def starts(self):
        return self.starts_and_ends[0]

    @property
    def ends(self):
        return self.starts_and_ends[1]

    @calculated_example_property
    class discarded(ExampleProperty):
        def begin(self):
            self.result = set()

        def finish(self):
            return frozenset(self.result)

        def stop_example(self, i, discarded):
            if discarded:
                self.result.add(i)

    @calculated_example_property
    class trivial(ExampleProperty):
        def begin(self):
            self.nontrivial = IntList.of_length(len(self.examples))
            self.result = set()

        def block(self, i):
            if not self.examples.blocks.trivial(i):
                self.nontrivial[self.example_stack[-1]] = 1

        def stop_example(self, i, discarded):
            if self.nontrivial[i]:
                if self.example_stack:
                    self.nontrivial[self.example_stack[-1]] = 1
            else:
                self.result.add(i)

        def finish(self):
            return frozenset(self.result)

    @calculated_example_property
    class parentage(ExampleProperty):
        def stop_example(self, i, discarded):
            if i > 0:
                self.result[i] = self.example_stack[-1]

    @calculated_example_property
    class depths(ExampleProperty):
        def begin(self):
            self.result = IntList.of_length(len(self.examples))

        def start_example(self, i, label_index):
            self.result[i] = len(self.example_stack)

    @calculated_example_property
    class label_indices(ExampleProperty):
        def start_example(self, i, label_index):
            self.result[i] = label_index

    @property
    def children(self):
        if self.__children is None:
            self.__children = [IntList() for _ in hrange(len(self))]
            for i, p in enumerate(self.parentage):
                if i > 0:
                    self.__children[p].append(i)
            # Replace empty children lists with a tuple to reduce
            # memory usage.
            for i, c in enumerate(self.__children):
                if not c:
                    self.__children[i] = ()
        return self.__children

    def __len__(self):
        return self.__length

    def __getitem__(self, i):
        assert isinstance(i, int)
        n = len(self)
        if i < -n or i >= n:
            raise IndexError("Index %d out of range [-%d, %d)" % (i, n, n))
        if i < 0:
            i += n
        return Example(self, i)


@attr.s(slots=True, frozen=True)
class Block(object):
    """Blocks track the flat list of lowest-level draws from the byte stream,
    within a single test run.

    Block-tracking allows the shrinker to try "low-level"
    transformations, such as minimizing the numeric value of an
    individual call to ``draw_bits``.
    """

    start = attr.ib()
    end = attr.ib()

    # Index of this block inside the overall list of blocks.
    index = attr.ib()

    # True if this block's byte values were forced by a write operation.
    # As long as the bytes before this block remain the same, modifying this
    # block's bytes will have no effect.
    forced = attr.ib(repr=False)

    # True if this block's byte values are all 0. Reading this flag can be
    # more convenient than explicitly checking a slice for non-zero bytes.
    all_zero = attr.ib(repr=False)

    @property
    def bounds(self):
        return (self.start, self.end)

    @property
    def length(self):
        return self.end - self.start

    @property
    def trivial(self):
        return self.forced or self.all_zero


class Blocks(object):
    """A lazily calculated list of blocks for a particular ``ConjectureResult``
    or ``ConjectureData`` object.

    Pretends to be a list containing ``Block`` objects but actually only
    contains their endpoints right up until the point where you want to
    access the actual block, at which point it is constructed.

    This is designed to be as space efficient as possible, so will at
    various points silently transform its representation into one
    that is better suited for the current access pattern.

    In addition, it has a number of convenience methods for accessing
    properties of the block object at index ``i`` that should generally
    be preferred to using the Block objects directly, as it will not
    have to allocate the actual object."""

    __slots__ = ("endpoints", "owner", "__blocks", "__count", "__sparse")

    def __init__(self, owner):
        self.owner = owner
        self.endpoints = IntList()
        self.__blocks = {}
        self.__count = 0
        self.__sparse = True

    def add_endpoint(self, n):
        """Add n to the list of endpoints."""
        assert isinstance(self.owner, ConjectureData)
        self.endpoints.append(n)

    def transfer_ownership(self, new_owner):
        """Used to move ``Blocks`` over to a ``ConjectureResult`` object
        when that is read to be used and we no longer want to keep the
        whole ``ConjectureData`` around."""
        assert isinstance(new_owner, ConjectureResult)
        self.owner = new_owner
        self.__check_completion()

    def start(self, i):
        """Equivalent to self[i].start."""
        i = self._check_index(i)

        if i == 0:
            return 0
        else:
            return self.end(i - 1)

    def end(self, i):
        """Equivalent to self[i].end."""
        return self.endpoints[i]

    def bounds(self, i):
        """Equivalent to self[i].bounds."""
        return (self.start(i), self.end(i))

    def all_bounds(self):
        """Equivalent to [(b.start, b.end) for b in self]."""
        prev = 0
        for e in self.endpoints:
            yield (prev, e)
            prev = e

    @property
    def last_block_length(self):
        return self.end(-1) - self.start(-1)

    def __len__(self):
        return len(self.endpoints)

    def __known_block(self, i):
        try:
            return self.__blocks[i]
        except (KeyError, IndexError):
            return None

    def trivial(self, i):
        """Equivalent to self.blocks[i].trivial."""
        if self.owner is not None:
            return self.start(i) in self.owner.forced_indices or not any(
                self.owner.buffer[self.start(i) : self.end(i)]
            )
        else:
            return self[i].trivial

    def _check_index(self, i):
        n = len(self)
        if i < -n or i >= n:
            raise IndexError("Index %d out of range [-%d, %d)" % (i, n, n))
        if i < 0:
            i += n
        return i

    def __getitem__(self, i):
        i = self._check_index(i)
        assert i >= 0
        result = self.__known_block(i)
        if result is not None:
            return result

        # We store the blocks as a sparse dict mapping indices to the
        # actual result, but this isn't the best representation once we
        # stop being sparse and want to use most of the blocks. Switch
        # over to a list at that point.
        if self.__sparse and len(self.__blocks) * 2 >= len(self):
            new_blocks = [None] * len(self)
            for k, v in self.__blocks.items():
                new_blocks[k] = v
            self.__sparse = False
            self.__blocks = new_blocks
            assert self.__blocks[i] is None

        start = self.start(i)
        end = self.end(i)

        # We keep track of the number of blocks that have actually been
        # instantiated so that when every block that could be instantiated
        # has been we know that the list is complete and can throw away
        # some data that we no longer need.
        self.__count += 1

        # Integrity check: We can't have allocated more blocks than we have
        # positions for blocks.
        assert self.__count <= len(self)
        result = Block(
            start=start,
            end=end,
            index=i,
            forced=start in self.owner.forced_indices,
            all_zero=not any(self.owner.buffer[start:end]),
        )
        try:
            self.__blocks[i] = result
        except IndexError:
            assert isinstance(self.__blocks, list)
            assert len(self.__blocks) < len(self)
            self.__blocks.extend([None] * (len(self) - len(self.__blocks)))
            self.__blocks[i] = result

        self.__check_completion()

        return result

    def __check_completion(self):
        """The list of blocks is complete if we have created every ``Block``
        object that we currently good and know that no more will be created.

        If this happens then we don't need to keep the reference to the
        owner around, and delete it so that there is no circular reference.
        The main benefit of this is that the gc doesn't need to run to collect
        this because normal reference counting is enough.
        """
        if self.__count == len(self) and isinstance(self.owner, ConjectureResult):
            self.owner = None

    def __iter__(self):
        for i in hrange(len(self)):
            yield self[i]

    def __repr__(self):
        parts = []
        for i in hrange(len(self)):
            b = self.__known_block(i)
            if b is None:
                parts.append("...")
            else:
                parts.append(repr(b))
        return "Block([%s])" % (", ".join(parts),)


class _Overrun(object):
    status = Status.OVERRUN

    def __repr__(self):
        return "Overrun"


Overrun = _Overrun()

global_test_counter = 0


MAX_DEPTH = 100


class DataObserver(object):
    """Observer class for recording the behaviour of a
    ConjectureData object, primarily used for tracking
    the behaviour in the tree cache."""

    def conclude_test(self, status, interesting_origin):
        """Called when ``conclude_test`` is called on the
        observed ``ConjectureData``, with the same arguments.

        Note that this is called after ``freeze`` has completed.
        """

    def draw_bits(self, n_bits, forced, value):
        """Called when ``draw_bits`` is called on on the
        observed ``ConjectureData``.
        * ``n_bits`` is the number of bits drawn.
        *  ``forced`` is True if the corresponding
           draw was forced or ``False`` otherwise.
        * ``value`` is the result that ``draw_bits`` returned.
        """

    def kill_branch(self):
        """Mark this part of the tree as not worth re-exploring."""


@attr.s(slots=True)
class ConjectureResult(object):
    """Result class storing the parts of ConjectureData that we
    will care about after the original ConjectureData has outlived its
    usefulness."""

    status = attr.ib()
    interesting_origin = attr.ib()
    buffer = attr.ib()
    blocks = attr.ib()
    output = attr.ib()
    extra_information = attr.ib()
    has_discards = attr.ib()
    target_observations = attr.ib()
    forced_indices = attr.ib(repr=False)
    examples = attr.ib(repr=False)

    index = attr.ib(init=False)

    def __attrs_post_init__(self):
        self.index = len(self.buffer)
        self.forced_indices = frozenset(self.forced_indices)


# Masks for masking off the first byte of an n-bit buffer.
# The appropriate mask is stored at position n % 8.
BYTE_MASKS = [(1 << n) - 1 for n in hrange(8)]
BYTE_MASKS[0] = 255


class ConjectureData(object):
    @classmethod
    def for_buffer(self, buffer, observer=None):
        buffer = hbytes(buffer)
        return ConjectureData(
            max_length=len(buffer),
            draw_bytes=lambda data, n: hbytes(buffer[data.index : data.index + n]),
            observer=observer,
        )

    def __init__(self, max_length, draw_bytes, observer=None):
        if observer is None:
            observer = DataObserver()
        assert isinstance(observer, DataObserver)
        self.observer = observer
        self.max_length = max_length
        self.is_find = False
        self._draw_bytes = draw_bytes
        self.overdraw = 0
        self.__block_starts = defaultdict(list)
        self.__block_starts_calculated_to = 0

        self.blocks = Blocks(self)
        self.buffer = bytearray()
        self.index = 0
        self.output = u""
        self.status = Status.VALID
        self.frozen = False
        global global_test_counter
        self.testcounter = global_test_counter
        global_test_counter += 1
        self.start_time = benchmark_time()
        self.events = set()
        self.forced_indices = set()
        self.interesting_origin = None
        self.draw_times = []
        self.max_depth = 0
        self.has_discards = False
        self.consecutive_discard_counts = []

        self.__result = None

        # Observations used for targeted search.  They'll be aggregated in
        # ConjectureRunner.generate_new_examples and fed to TargetSelector.
        self.target_observations = {}

        # Normally unpopulated but we need this in the niche case
        # that self.as_result() is Overrun but we still want the
        # examples for reporting purposes.
        self.__examples = None

        # We want the top level example to have depth 0, so we start
        # at -1.
        self.depth = -1
        self.__example_record = ExampleRecord()

        self.extra_information = ExtraInformation()

        self.start_example(TOP_LABEL)

    def __repr__(self):
        return "ConjectureData(%s, %d bytes%s)" % (
            self.status.name,
            len(self.buffer),
            ", frozen" if self.frozen else "",
        )

    def as_result(self):
        """Convert the result of running this test into
        either an Overrun object or a ConjectureResult."""

        assert self.frozen
        if self.status == Status.OVERRUN:
            return Overrun
        if self.__result is None:
            self.__result = ConjectureResult(
                status=self.status,
                interesting_origin=self.interesting_origin,
                buffer=self.buffer,
                examples=self.examples,
                blocks=self.blocks,
                output=self.output,
                extra_information=self.extra_information
                if self.extra_information.has_information()
                else None,
                has_discards=self.has_discards,
                target_observations=self.target_observations,
                forced_indices=self.forced_indices,
            )
            self.blocks.transfer_ownership(self.__result)
        return self.__result

    def __assert_not_frozen(self, name):
        if self.frozen:
            raise Frozen("Cannot call %s on frozen ConjectureData" % (name,))

    def note(self, value):
        self.__assert_not_frozen("note")
        if not isinstance(value, text_type):
            value = unicode_safe_repr(value)
        self.output += value

    def draw(self, strategy, label=None):
        if self.is_find and not strategy.supports_find:
            raise InvalidArgument(
                (
                    "Cannot use strategy %r within a call to find (presumably "
                    "because it would be invalid after the call had ended)."
                )
                % (strategy,)
            )

        if strategy.is_empty:
            self.mark_invalid()

        if self.depth >= MAX_DEPTH:
            self.mark_invalid()

        return self.__draw(strategy, label=label)

    def __draw(self, strategy, label):
        at_top_level = self.depth == 0
        if label is None:
            label = strategy.label
        self.start_example(label=label)
        try:
            if not at_top_level:
                return strategy.do_draw(self)
            else:
                try:
                    strategy.validate()
                    start_time = benchmark_time()
                    try:
                        return strategy.do_draw(self)
                    finally:
                        self.draw_times.append(benchmark_time() - start_time)
                except BaseException as e:
                    mark_for_escalation(e)
                    raise
        finally:
            self.stop_example()

    def start_example(self, label):
        self.__assert_not_frozen("start_example")
        self.depth += 1
        # Logically it would make sense for this to just be
        # ``self.depth = max(self.depth, self.max_depth)``, which is what it used to
        # be until we ran the code under tracemalloc and found a rather significant
        # chunk of allocation was happening here. This was presumably due to varargs
        # or the like, but we didn't investigate further given that it was easy
        # to fix with this check.
        if self.depth > self.max_depth:
            self.max_depth = self.depth
        self.__example_record.start_example(label)
        self.consecutive_discard_counts.append(0)

    def stop_example(self, discard=False):
        if self.frozen:
            return
        self.consecutive_discard_counts.pop()
        if discard:
            self.has_discards = True
        self.depth -= 1
        assert self.depth >= -1
        self.__example_record.stop_example(discard)
        if self.consecutive_discard_counts:
            # We block long sequences of discards. This helps us avoid performance
            # problems where there is rejection sampling. In particular tests which
            # have a very small actual state space but use rejection sampling will
            # play badly with generate_novel_prefix() in DataTree, and will end up
            # generating very long tests with long runs of the rejection sample.
            if discard:
                self.consecutive_discard_counts[-1] += 1
                # 20 is a fairly arbitrary limit chosen mostly so that all of the
                # existing tests passed under it. Essentially no reasonable
                # generation should hit this limit when running in purely random
                # mode, but unreasonable generation is fairly widespread, and our
                # manipulation of the bitstream can make it more likely.
                if self.consecutive_discard_counts[-1] > 20:
                    self.mark_invalid()
            else:
                self.consecutive_discard_counts[-1] = 0
        if discard:
            # Once we've discarded an example, every test case starting with
            # this prefix contains discards. We prune the tree at that point so
            # as to avoid future test cases bothering with this region, on the
            # assumption that some example that you could have used instead
            # there would *not* trigger the discard. This greatly speeds up
            # test case generation in some cases, because it allows us to
            # ignore large swathes of the search space that are effectively
            # redundant.
            #
            # A scenario that can cause us problems but which we deliberately
            # have decided not to support is that if there are side effects
            # during data generation then you may end up with a scenario where
            # every good test case generates a discard because the discarded
            # section sets up important things for later. This is not terribly
            # likely and all that you see in this case is some degradation in
            # quality of testing, so we don't worry about it.
            #
            # Note that killing the branch does *not* mean we will never
            # explore below this point, and in particular we may do so during
            # shrinking. Any explicit request for a data object that starts
            # with the branch here will work just fine, but novel prefix
            # generation will avoid it, and we can use it to detect when we
            # have explored the entire tree (up to redundancy).

            self.observer.kill_branch()

    def note_event(self, event):
        self.events.add(event)

    @property
    def examples(self):
        assert self.frozen
        if self.__examples is None:
            self.__examples = Examples(record=self.__example_record, blocks=self.blocks)
        return self.__examples

    def freeze(self):
        if self.frozen:
            assert isinstance(self.buffer, hbytes)
            return
        self.finish_time = benchmark_time()
        assert len(self.buffer) == self.index

        # Always finish by closing all remaining examples so that we have a
        # valid tree.
        while self.depth >= 0:
            self.stop_example()

        self.__example_record.freeze()

        self.frozen = True

        self.buffer = hbytes(self.buffer)
        self.events = frozenset(self.events)
        del self._draw_bytes
        self.observer.conclude_test(self.status, self.interesting_origin)

    def draw_bits(self, n, forced=None):
        """Return an ``n``-bit integer from the underlying source of
        bytes. If ``forced`` is set to an integer will instead
        ignore the underlying source and simulate a draw as if it had
        returned that integer."""
        self.__assert_not_frozen("draw_bits")
        if n == 0:
            return 0
        assert n > 0
        n_bytes = bits_to_bytes(n)
        self.__check_capacity(n_bytes)

        if forced is not None:
            buf = bytearray(int_to_bytes(forced, n_bytes))
        else:
            buf = bytearray(self._draw_bytes(self, n_bytes))
        assert len(buf) == n_bytes

        # If we have a number of bits that is not a multiple of 8
        # we have to mask off the high bits.
        buf[0] &= BYTE_MASKS[n % 8]
        buf = hbytes(buf)
        result = int_from_bytes(buf)

        self.observer.draw_bits(n, forced is not None, result)
        self.__example_record.draw_bits(n, forced)

        initial = self.index

        self.buffer.extend(buf)
        self.index = len(self.buffer)

        if forced is not None:
            self.forced_indices.update(hrange(initial, self.index))

        self.blocks.add_endpoint(self.index)

        assert bit_length(result) <= n
        return result

    def draw_bytes(self, n):
        """Draw n bytes from the underlying source."""
        return int_to_bytes(self.draw_bits(8 * n), n)

    def write(self, string):
        """Write ``string`` to the output buffer."""
        self.__assert_not_frozen("write")
        string = hbytes(string)
        if not string:
            return
        self.draw_bits(len(string) * 8, forced=int_from_bytes(string))
        return self.buffer[-len(string) :]

    def __check_capacity(self, n):
        if self.index + n > self.max_length:
            self.mark_overrun()

    def conclude_test(self, status, interesting_origin=None):
        assert (interesting_origin is None) or (status == Status.INTERESTING)
        self.__assert_not_frozen("conclude_test")
        self.interesting_origin = interesting_origin
        self.status = status
        self.freeze()
        raise StopTest(self.testcounter)

    def mark_interesting(self, interesting_origin=None):
        self.conclude_test(Status.INTERESTING, interesting_origin)

    def mark_invalid(self):
        self.conclude_test(Status.INVALID)

    def mark_overrun(self):
        self.conclude_test(Status.OVERRUN)


def bits_to_bytes(n):
    """The number of bytes required to represent an n-bit number.
    Equivalent to (n + 7) // 8, but slightly faster. This really is
    called enough times that that matters."""
    return (n + 7) >> 3
