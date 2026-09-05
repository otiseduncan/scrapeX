from scrapex.navigator_graph import NavigationGraph, fingerprint
from scrapex.navigator_observation import Observation, ObservationNode


def _obs(url, title, names):
    return Observation(
        url=url, title=title,
        elements=[ObservationNode(ref=f"e{i}", role="link", name=n) for i, n in enumerate(names)],
    )


def test_fingerprint_is_stable_for_identical_state():
    a = _obs("https://x/page?nonce=1", "Page", ["A", "B"])
    b = _obs("https://x/page?nonce=2", "Page", ["A", "B"])
    assert fingerprint(a) == fingerprint(b)  # query-string churn ignored


def test_fingerprint_differs_for_different_content():
    a = _obs("https://x/page", "Page", ["A"])
    b = _obs("https://x/page", "Page", ["B"])
    assert fingerprint(a) != fingerprint(b)


def test_record_marks_first_visit_as_new_state():
    graph = NavigationGraph()
    result = graph.record(_obs("https://x/a", "A", ["1"]))
    assert result.is_new_state is True
    assert result.visit_count == 1
    assert result.backtrack_available is False  # first state, no parent


def test_record_tracks_parent_chain_and_backtrack_target():
    graph = NavigationGraph()
    graph.record(_obs("https://x/a", "A", ["1"]))
    result = graph.record(_obs("https://x/b", "B", ["2"]), action={"action": "click", "ref": "e0"})
    assert result.backtrack_available is True
    parent = graph.backtrack_target()
    assert parent is not None
    assert parent.fingerprint == fingerprint(_obs("https://x/a", "A", ["1"]))


def test_repeated_state_triggers_loop_warning():
    graph = NavigationGraph(max_state_visits=2)
    obs_a = _obs("https://x/a", "A", ["1"])
    obs_b = _obs("https://x/b", "B", ["2"])
    graph.record(obs_a)
    graph.record(obs_b)
    graph.record(obs_a)  # visit 2, at the limit -- no warning yet
    result = graph.record(obs_b)  # revisits b a second time -> visit_count 2, still at limit
    assert result.visit_count == 2
    result = graph.record(obs_a)  # third visit to a -> over the limit
    assert result.visit_count == 3
    assert result.loop_warning is not None


def test_repeated_identical_action_triggers_warning():
    graph = NavigationGraph(max_repeated_action=1)
    start = _obs("https://x/start", "Start", ["go"])
    dead_end = _obs("https://x/dead-end", "Dead end", ["go"])
    graph.record(start)
    action = {"action": "click", "ref": "e0"}
    graph.record(dead_end, action=action)
    # Back to start, try the exact same action again -- should warn this time.
    graph.record(start)
    result = graph.record(dead_end, action=action)
    assert result.repeated_action_warning is not None


def test_new_action_from_same_state_does_not_warn():
    graph = NavigationGraph(max_repeated_action=1)
    start = _obs("https://x/start", "Start", ["go", "other"])
    graph.record(start)
    graph.record(_obs("https://x/branch1", "B1", ["x"]), action={"action": "click", "ref": "e0"})
    graph.record(start)
    result = graph.record(_obs("https://x/branch2", "B2", ["y"]), action={"action": "click", "ref": "e1"})
    assert result.repeated_action_warning is None


def test_round_trip_serialization():
    graph = NavigationGraph()
    graph.record(_obs("https://x/a", "A", ["1"]))
    graph.record(_obs("https://x/b", "B", ["2"]), action={"action": "click", "ref": "e0"})
    restored = NavigationGraph.from_dict(graph.to_dict())
    assert restored.history == graph.history
    assert set(restored.visited) == set(graph.visited)
    for fp in graph.visited:
        assert restored.visited[fp].parent_fingerprint == graph.visited[fp].parent_fingerprint
        assert restored.visited[fp].visit_count == graph.visited[fp].visit_count


def test_from_dict_handles_missing_or_malformed_input():
    assert NavigationGraph.from_dict({}).history == []
    assert NavigationGraph.from_dict(None).history == []
    assert NavigationGraph.from_dict({"visited": "not-a-dict"}).visited == {}
