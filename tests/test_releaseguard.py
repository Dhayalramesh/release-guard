import pytest
from pydantic import ValidationError

from releaseguard import ChangeRecord, DeployError, Deployer


def rec(version, risk="low", approver="Asha"):
    return ChangeRecord(version=version, risk=risk, approver=approver,
                        rollback_plan="redeploy previous tag", description="test")


@pytest.fixture
def dep(tmp_path):
    d = Deployer(state_dir=tmp_path, ports={"staging": 18101, "production": 18102},
                 retries=5, backoff=0.2)
    yield d
    d.stop_all()


def test_rejects_bad_version():
    with pytest.raises(ValidationError):
        rec("latest")


def test_requires_approver():
    with pytest.raises(ValidationError):
        rec("v1.0.0", approver="")


def test_rejects_unknown_risk_level():
    with pytest.raises(ValidationError):
        rec("v1.0.0", risk="yolo")


def test_good_release_goes_live(dep):
    out = dep.deploy("staging", rec("v1.0.0"))
    assert out == {"status": "success", "live": "v1.0.0"}
    assert dep.live_version("staging") == "v1.0.0"


def test_broken_release_rolls_back_automatically(dep):
    dep.deploy("staging", rec("v1.0.0"))
    out = dep.deploy("staging", rec("v1.1.0"), simulate_broken=True)
    assert out == {"status": "rolled_back", "live": "v1.0.0"}
    assert dep.live_version("staging") == "v1.0.0"
    assert [r[4] for r in dep.history("staging")] == ["success", "failed", "restored"]


def test_first_release_broken_leaves_nothing_live(dep):
    out = dep.deploy("staging", rec("v1.0.0"), simulate_broken=True)
    assert out["status"] == "failed"
    assert dep.live_version("staging") is None


def test_production_blocked_until_staged(dep):
    with pytest.raises(DeployError):
        dep.deploy("production", rec("v1.0.0"))


def test_promotion_after_staging(dep):
    dep.deploy("staging", rec("v1.0.0"))
    out = dep.deploy("production", rec("v1.0.0"))
    assert out["status"] == "success"


def test_manual_rollback(dep):
    dep.deploy("staging", rec("v1.0.0"))
    dep.deploy("staging", rec("v1.1.0"))
    assert dep.rollback("staging") == "v1.0.0"
    assert dep.live_version("staging") == "v1.0.0"
