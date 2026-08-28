from athena.protocol import ids


def test_new_id_unique():
    generated = {ids.new_id("task") for _ in range(100)}
    assert len(generated) == 100


def test_common_prefixes():
    assert ids.new_id("task").startswith("task_")
    assert ids.new_id("sess").startswith("sess_")
    assert ids.new_id("msg").startswith("msg_")


def test_fake_id_deterministic():
    assert ids.fake_id("task", 7) == "task_0000000007"
    assert ids.fake_id("task", 7) == ids.fake_id("task", 7)
