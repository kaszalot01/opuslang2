from __future__ import annotations

import prettyprinter
prettyprinter.install_extras(['dataclasses'])

from copy import deepcopy
from functools import partial, reduce
from typing import Optional, Any, List

from opuslang2.parser import parser
from lark import Transformer, v_args
import lark
from opus.lang import ir
from dataclasses import dataclass, field

import random

@dataclass(frozen=True, order=True)
class Bid:
    level: int
    color: ir.Suit
    meta: Optional[Any] = field(repr=False, compare=False)  # Skip meta in repr, cmp and hash

    @classmethod
    def from_str(cls, s: str) -> Bid:
        level = int(s[0])
        suit = ir.Suit(None, s[1:])
        return Bid(level, suit, None)


def _is_prefix(p, l):
    for prefix_el, list_el in zip(p, l):
        if prefix_el != list_el:
            return False
    return True


@dataclass
class BidHistory:
    sequence: List[Bid]
    meta: Optional[Any] = field(repr=False, default=None)

    def __contains__(self, item):
        if isinstance(item, BidHistory):
            item = item.sequence
        if isinstance(item, str):
            item = BidHistory.from_str(item).sequence

        return _is_prefix(item, self.sequence)

    def __add__(self, other):
        if isinstance(other, Bid):
            new_sequence = self.sequence.copy()
            new_sequence.append(other)
            return BidHistory(new_sequence, self.meta)
        else:
            raise ValueError(f"Add operator between BidHistory and {type(other)} not supported")

    @classmethod
    def from_str(cls, s: str) -> BidHistory:
        return BidHistory(list(map(Bid.from_str, s.split("-"))), None)


@dataclass
class BidExpression:
    prefix: Bid
    conditions: List[Condition]
    meta: Optional[Any] = field(repr=False)


@dataclass
class Branch:
    prefix: BidHistory
    continuations: List[BidExpression]
    meta: Optional[Any] = field(repr=False)

    def all_conditions_sorted(self):
        to_sort = []
        for bid_expr in self.continuations:
            for cond in bid_expr.conditions:
                # abusing tuple sort very, very hard
                # the correct sort order is by priority, ascending; then by suit, descending
                # Suit compare can't be flipped, so we flip the priority order, then reverse whole list
                num_val = -cond.priority if cond.priority is not None else -999_999

                # Comparison between conditions is undefined
                # To work around it we add a random uniquifier to comparing tuple

                uniquifier = random.randrange(999999999999)
                to_sort.append((num_val, bid_expr.prefix, uniquifier, cond))
        to_sort.sort(reverse=True)
        return ((c, bid) for _, bid, _,  c in to_sort)


@dataclass
class LogicSuit:
    type: str
    lhs: ir.Suit
    rhs: ir.Suit
    meta: Optional[Any] = field(repr=False, compare=False)

    @staticmethod
    def resolve_logical_suits(expr: ir.BinaryExpr):
        if isinstance(expr.lhs, ir.BinaryExpr):
            expr.lhs = LogicSuit.resolve_logical_suits(expr.lhs)
        if isinstance(expr.rhs, ir.BinaryExpr):
            expr.rhs = LogicSuit.resolve_logical_suits(expr.rhs)

        # Probably can assume only one side is LogicSuit
        if isinstance(expr.rhs, LogicSuit) and isinstance(expr.lhs, LogicSuit):
            raise NotImplementedError("Only one side of expression can be a logic suit")

        if isinstance(logic_suit := getattr(expr.lhs, "child", None), LogicSuit):
            variant1 = deepcopy(expr)
            variant2 = deepcopy(expr)
            variant1.lhs = logic_suit.lhs
            variant2.lhs = logic_suit.rhs

            return ir.BinaryExpr(expr.meta, variant1, logic_suit.type, variant2)

        elif isinstance(getattr(expr.rhs, "child", None), LogicSuit):
            variant1 = deepcopy(expr)
            variant2 = deepcopy(expr)
            variant1.lhs = logic_suit.lhs
            variant2.lhs = logic_suit.rhs

            return ir.BinaryExpr(expr.meta, variant1, logic_suit.type, variant2)
        else:
            return expr


@dataclass
class Condition:
    expr: Any
    priority: Optional[int]  # None means no priority == infinity
    meta: Optional[Any] = field(repr=False)

    def children_iterator(self):
        return ir.one_shot_gen(self.expr)


def meta_kw(func):
    def f(meta, *args, **kwargs):
        return func(*args, meta=meta, **kwargs)

    return f


@v_args(inline=True, meta=True)
class CompileTransformer(Transformer):

    @staticmethod
    @meta_kw
    def point_range(range_gen, color=None, meta=None):
        if color is not None:
            raise NotImplementedError("Colored ranges not supported")

        # Now we can assume child is an ir.Expr for points
        return range_gen(ir.Atom("SUIT_POINTS", meta, ir.Suit(meta, "@")))

    # range methods return functions
    # arguments specify value to compare

    @staticmethod
    @meta_kw
    def range(lower, upper, meta=None):
        def f(val):
            upper_expr = ir.BinaryExpr(
                meta,
                val,
                "<=",
                upper
            )

            lower_expr = ir.BinaryExpr(
                meta,
                ir.Atom("SUIT_POINTS", meta, ir.Suit(meta, "@")),
                ">=",
                lower
            )

            result = ir.BinaryExpr(
                meta,
                lower_expr,
                "and",
                upper_expr
            )

            return result
        return f

    @staticmethod
    @meta_kw
    def or_fewer(upper, meta=None):
        def f(val):
            upper_expr = ir.BinaryExpr(
                meta,
                val,
                "<=",
                upper
            )

            return upper_expr
        return f

    @staticmethod
    @meta_kw
    def or_more(lower, meta=None):
        def f(val):
            lower_expr = ir.BinaryExpr(
                meta,
                val,
                ">=",
                lower
            )
            return lower_expr

        return f

    @staticmethod
    @meta_kw
    def exact(lower, meta=None):
        def f(val):
            lower_expr = ir.BinaryExpr(
                meta,
                val,
                "==",
                lower
            )
            return lower_expr

        return f

    @staticmethod
    @meta_kw
    def binary(lhs, op, rhs, meta=None):
        return LogicSuit.resolve_logical_suits(ir.BinaryExpr(meta, lhs, op, rhs))

    @staticmethod
    @meta_kw
    def unary(_op, operand, meta=None):
        # op is always "not" for now
        return ir.Atom("NEG", meta, operand)

    @staticmethod
    @meta_kw
    def cmp(lhs, op, rhs, meta=None):
        return LogicSuit.resolve_logical_suits(ir.BinaryExpr(meta, lhs, str(op), rhs))

    @staticmethod
    @meta_kw
    def count_expr(range_gen, suit, meta=None):
        return LogicSuit.resolve_logical_suits(range_gen(ir.Atom("SUIT_CARDS", meta, suit)))

    @staticmethod
    @meta_kw
    def and_suit(lhs, rhs, meta=None):
        return LogicSuit("and", lhs, rhs, meta=meta)

    @staticmethod
    @meta_kw
    def or_suit(lhs, rhs, meta=None):
        return LogicSuit("or", lhs, rhs, meta=meta)

    @staticmethod
    @meta_kw
    def and_op(meta=None):
        return "and"

    @staticmethod
    @meta_kw
    def or_op(meta=None):
        return "or"

    @staticmethod
    @meta_kw
    def prioritized(*args, meta=None):
        *conditions, priority = args
        head, *tail = conditions
        return Condition(
            reduce(lambda acc, x: ir.BinaryExpr(meta, acc, "and", x), tail, head),
            priority,
            meta
        )

    @staticmethod
    @meta_kw
    def unprioritized(*args, meta=None):
        head, *tail = args
        return Condition(
            reduce(lambda acc, x: ir.BinaryExpr(meta, acc, "and", x), tail, head),
            None,
            meta
        )

    @staticmethod
    @meta_kw
    def bid_body(*args, meta=None):
        return args

    @staticmethod
    @meta_kw
    def bid_level(*args, meta=None):
        return Bid(*args, meta)

    @staticmethod
    @meta_kw
    def bid_def(child, meta=None):
        return child

    @staticmethod
    @meta_kw
    def bid(*args, meta=None):
        return BidExpression(*args, meta=meta)

    @staticmethod
    @meta_kw
    def opening(meta=None):
        return BidHistory([], meta)

    @staticmethod
    @meta_kw
    def continuation(*children, meta=None):
        return BidHistory(children, meta=None)

    @staticmethod
    @meta_kw
    def branch_body(*children, meta=None):
        return children

    @staticmethod
    @meta_kw
    def branch(history, bid_expressions, meta=None):
        return Branch(history, bid_expressions, meta)

    @staticmethod
    @meta_kw
    def start(*children, meta=None):
        return children

    trump_suit = ir.Suit
    suit = ir.Suit

    NUMBER = int
    variable = partial(ir.Atom, "VAR")


placeholder = object()


def _find(iterable, predicate, default=placeholder):
    for el in iterable:
        if predicate(el):
            return el
    if default is not placeholder:
        return default
    raise ValueError("No item matched the supplied predicate")


def build_branch(branch: Branch, rest: List[Branch]) -> List[ir.Branch]:
    continuation_dict = {}

    for continuation_bid in branch.continuations:
        continuation_branch = _find(rest, lambda b: b.prefix == branch.prefix + continuation_bid.prefix, None)
        if continuation_branch is not None:
            compiled = build_branch(continuation_branch, rest)
            continuation_dict[continuation_bid.prefix] = compiled
        else:
            continuation_dict[continuation_bid.prefix] = []

    result = []

    for condition, bid in branch.all_conditions_sorted():
        new_branch = ir.Branch(
            # Branches may be shared. Closest equivalent of ir.Branch is List[BranchExpr] but no way to get that now
            meta=condition.meta,
            test=condition.expr,
            bids=[ir.BidStatement(bid.meta, bid.level, bid.color)],
            children=continuation_dict[bid],
            end=None  # TODO add end markers
        )
        result.append(new_branch)

    return result


if __name__ == '__main__':
    test = open('../blas.ol2').read()

    tree = parser.parse(test)
    res = CompileTransformer().transform(tree)
    from prettyprinter import pprint
    pprint(build_branch(res[0], res))
