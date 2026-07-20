import pytest


hypothesis = pytest.importorskip('hypothesis')
from hypothesis import given, settings, strategies as st

from blockchain.blockchain import Blockchain
from denarius_ledger import ChainState
from denarius_protocol import (
    MAX_SUPPLY_ATOMIC,
    MAX_TARGET,
    canonical_json_bytes,
    target_from_hex,
    target_to_hex,
)


@given(st.integers(min_value=1, max_value=MAX_SUPPLY_ATOMIC))
@settings(max_examples=250, deadline=None)
def test_denarii_amount_format_round_trips_every_atomic_value(atomic_value):
    blockchain = Blockchain()
    assert blockchain.parse_amount(blockchain.format_amount(atomic_value)) == atomic_value


@given(st.integers(min_value=1, max_value=MAX_TARGET))
@settings(max_examples=250, deadline=None)
def test_target_encoding_is_a_bijection(target):
    assert target_from_hex(target_to_hex(target)) == target


@given(
    st.dictionaries(
        st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=20),
        st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=40)),
        max_size=20,
    )
)
@settings(max_examples=250, deadline=None)
def test_canonical_json_is_independent_of_dictionary_insertion_order(mapping):
    reversed_mapping = dict(reversed(list(mapping.items())))
    assert canonical_json_bytes(mapping) == canonical_json_bytes(reversed_mapping)


@given(
    st.dictionaries(st.text(min_size=1, max_size=40), st.integers(min_value=0), max_size=20),
    st.dictionaries(st.text(min_size=1, max_size=40), st.integers(min_value=0), max_size=20),
)
@settings(max_examples=150, deadline=None)
def test_indexed_chain_state_serialization_round_trips(balances, nonces):
    state = ChainState()
    state.balances.update(balances)
    state.nonces.update(nonces)
    assert ChainState.from_dict(state.as_dict()).as_dict() == state.as_dict()


json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=100),
)


@given(st.lists(json_scalars, min_size=7, max_size=7))
@settings(max_examples=500, deadline=None)
def test_generated_malformed_transactions_cannot_crash_validation(fields):
    blockchain = Blockchain()
    result = blockchain.submit_transaction(
        fields[0],
        fields[1],
        fields[2],
        fields[3],
        fields[4],
        fields[5],
        fee=fields[6],
        relay=False,
    )
    assert result is False
