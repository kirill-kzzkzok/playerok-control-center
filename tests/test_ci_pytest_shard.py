"""The CI split is exhaustive, disjoint, deterministic and keeps modules whole."""

import pytest

from tools.ci_pytest_shard import partition


def test_collected_id_partition_without_loss_or_duplicate():
    nodeids = [
        f"tests/test_{module}.py::test_{test}[{test}]" for module in range(11) for test in range(module + 1)
    ]
    groups = partition(nodeids, 3)
    assert partition(list(reversed(nodeids)), 3) == groups
    modules = [module for group in groups for module in group]
    assert len(modules) == len(set(modules)) == 11
    selected = [[ident for ident in nodeids if ident.split("::", 1)[0] in group] for group in groups]
    assert sorted(ident for group in selected for ident in group) == sorted(nodeids)
    assert max(map(len, selected)) - min(map(len, selected)) <= 2


@pytest.mark.parametrize("ids,count", [(["same", "same"], 3), (["one"], 0), (["one"], -1)])
def test_partition_rejects_invalid_collection_or_count(ids, count):
    with pytest.raises(ValueError):
        partition(ids, count)
