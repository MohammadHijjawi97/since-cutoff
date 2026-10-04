from since_cutoff.checker import _Snippet


def test_annotated_assignment():
    snippet = _Snippet(
        "import toylib\nclient: toylib.Client = None\nc: object = toylib.Client()",
        ("toylib",),
    )
    assert "client" in snippet.bound
    assert "c" in snippet.bound


def test_walrus_operator():
    snippet = _Snippet(
        "import toylib\nif client := toylib.Client():\n    pass",
        ("toylib",),
    )
    assert "client" in snippet.bound


def test_for_loop():
    snippet = _Snippet(
        "import toylib\nfor client in toylib.clients:\n    pass",
        ("toylib",),
    )
    assert "client" in snippet.bound


def test_comprehension():
    snippet = _Snippet(
        "import toylib\nclients = [client for client in toylib.clients]",
        ("toylib",),
    )
    assert "client" in snippet.bound


def test_with_statement():
    snippet = _Snippet(
        "import toylib\nwith toylib.Client() as c:\n    pass",
        ("toylib",),
    )
    assert "c" in snippet.bound


def test_negative_propagation_branches():
    snippet = _Snippet(
        "import toylib\n"
        "n: int = 0\n"
        "with open('r') as f, toylib.Client() as c:\n"
        "    pass\n"
        "with toylib.Client():\n"
        "    pass\n"
        "def helper(x: int):\n"
        "    return 1\n"
        "class Base:\n"
        "    pass\n"
        "class Mine(Base):\n"
        "    pass",
        ("toylib",),
    )

    assert "n" not in snippet.bound
    assert "f" not in snippet.bound
    assert "c" in snippet.bound
    assert "helper" not in snippet.bound
    assert "x" not in snippet.bound
    assert "Base" not in snippet.bound
    assert "Mine" not in snippet.bound


def test_involves_target_declared_type():
    snippet = _Snippet(
        "import toylib\nx: toylib.Opts = {}\nn: int = 0",
        ("toylib",),
    )
    assert snippet.involves_target(1, 17)
    assert not snippet.involves_target(2, 9)


def test_involves_target_return_annotation():
    snippet = _Snippet(
        "import toylib\ndef get_opts() -> toylib.Opts:\n    return {}\nn = 1",
        ("toylib",),
    )
    assert snippet.involves_target(2, 4)
    assert not snippet.involves_target(3, 4)


def test_tuple_assignment():
    snippet = _Snippet(
        "import toylib\na, b = toylib.Client(), 1",
        ("toylib",),
    )
    # The issue allows every element of a tuple target to be bound.
    assert "a" in snippet.bound
    assert "b" in snippet.bound


def test_involves_target_does_not_cross_argument_boundary():
    snippet = _Snippet(
        "import toylib\nclient = toylib.Client()\nclient.send(datetime.utcnow())",
        ("toylib",),
    )
    assert not snippet.involves_target(2, 12)


def test_invalid_python_code():
    snippet = _Snippet(
        "import toylib\nthis is not valid python !!!",
        ("toylib",),
    )
    assert snippet.tree is None
    assert snippet.bound == set()
    assert not snippet.involves_target(1, 0)


def test_import_module_at():
    snippet = _Snippet(
        "import toylib\nfrom datetime import datetime",
        ("toylib",),
    )
    assert snippet.import_module_at(0) == "toylib"
    assert snippet.import_module_at(1) == "datetime"
    assert snippet.import_module_at(2) is None


def test_statement_at():
    snippet = _Snippet(
        "import toylib\nclient = toylib.Client()\n",
        ("toylib",),
    )
    assert snippet.statement_at(0) == ((0, 0), "import toylib")
    assert snippet.statement_at(1) == ((1, 1), "client = toylib.Client()")
    assert snippet.statement_at(2) == ((2, 2), "")

    nested = _Snippet(
        "if True:\n    y = 1",
        ("toylib",),
    )
    assert nested.statement_at(1) == ((1, 1), "y = 1")
    assert nested.statement_at(10) == ((10, 10), "")


def test_function_argument_annotation():
    snippet = _Snippet(
        "import toylib\ndef send(client: toylib.Client):\n    pass",
        ("toylib",),
    )
    assert "client" in snippet.bound


def test_function_return_annotation():
    snippet = _Snippet(
        "import toylib\ndef get_client() -> toylib.Client:\n    ...",
        ("toylib",),
    )
    assert "get_client" in snippet.bound


def test_function_return_value():
    snippet = _Snippet(
        "import toylib\ndef make():\n    return toylib.Client()",
        ("toylib",),
    )
    assert "make" in snippet.bound


def test_class_inheritance():
    snippet = _Snippet(
        "import toylib\nclass MyClient(toylib.Client):\n    pass",
        ("toylib",),
    )
    assert "MyClient" in snippet.bound


def test_involves_target_error_inside_argument():
    snippet = _Snippet(
        "import toylib\nclient = toylib.Client()\nclient.send(client.missing())",
        ("toylib",),
    )
    assert snippet.involves_target(2, 19)
