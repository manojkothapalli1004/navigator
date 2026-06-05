from navigator.ids import new_id

def test_new_id_has_prefix_and_is_unique():
    a = new_id("run")
    b = new_id("run")
    assert a.startswith("run-")
    assert a != b
    assert len(a) > len("run-")
