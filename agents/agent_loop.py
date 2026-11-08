"""
THE AGENT LOOP - the one loop the agents run (M2, M3; and the M4 experiment,
experiments/m4_model_coordinator.py).

Each agent supplies two steps:
    decide(state)  asks the model for ONE action: a tool and its arguments
    act(state)     runs that tool, checks the result in code, and sets
                   state["done"] when the goal is reached, the model says
                   finish, or the step budget is used
and this module runs them:  decide -> act -> decide -> ... -> done.

The loop is a LangGraph graph. Without langgraph installed, the same two
functions run in a plain while-loop - same result (the tests compare both).
"""

try:
    from langgraph.graph import END, StateGraph
except ImportError:
    StateGraph = None


def run(state, decide, act, state_type, max_steps, use_graph=True):
    """Run the loop to the end. Returns (final state, "langgraph" | "loop")."""
    if not (use_graph and StateGraph is not None):
        return run_loop(state, decide, act), "loop"
    g = StateGraph(state_type)
    g.add_node("decide", decide)
    g.add_node("act", act)
    g.set_entry_point("decide")
    g.add_conditional_edges("decide", lambda s: "end" if s["done"] else "act",
                            {"act": "act", "end": END})
    g.add_conditional_edges("act", lambda s: "end" if s["done"] else "decide",
                            {"decide": "decide", "end": END})
    final = g.compile().invoke(state, {"recursion_limit": 4 * max_steps + 4})
    return final, "langgraph"


def run_loop(state, decide, act):
    """The same loop without langgraph."""
    while not state["done"]:
        state = {**state, **decide(state)}
        if state["done"]:
            break
        state = {**state, **act(state)}
    return state


def ask_model(ask, system, user):
    """One model call -> the reply as a dict. A reply that is not JSON, or an
    empty answer, is a wasted step ({}); an outage (no provider, every
    provider failed) is raised, for the agent to stop on."""
    try:
        reply = ask(system, user)
    except ValueError:
        return {}
    except RuntimeError as e:
        if type(e).__name__ != "EmptyAnswer":
            raise
        return {}
    return reply if isinstance(reply, dict) else {}


def parse_action(reply):
    """A reply -> (action, args, thought). Accepts {"action", "args",
    "thought"} and a native tool call {"name", "arguments"}; drops the
    prefix gpt-oss puts on tool names it was trained on
    ("repo_browser.search" -> "search")."""
    if "action" not in reply and "name" in reply:
        reply = {"action": reply.get("name"),
                 "args": reply.get("arguments") or {}}
    action = str(reply.get("action", "")).strip().lower().split(".")[-1]
    args = reply.get("args") if isinstance(reply.get("args"), dict) else {}
    return action, args, str(reply.get("thought", ""))[:300]
