# SPDX-License-Identifier: GPL-3.0-or-later
"""Which joints a mechanism defines, for the color of their control bones.

Oscar, 2026-09-24: "Blue for a fully defined joint, and red for joints
with one of the ends not fully defined. So a 4 bar linkage would have all
blue joints because the full loop is fully defined, but a revolute joint
for a saw blade or something would be red because its rotation is not
defined by another joint, so it will become a red control bone."

The bodies and the joints between them make a graph, with every grounded
group as one body: the world. A joint in a closed chain is defined: a loop,
or two joints that a coupling ties together. A gear pair, a rack and its
pinion, and a cam and its follower close a chain through the teeth or the
contact. A joint that is the only way to its body, a bridge of the graph,
is free: nothing else moves it. A joint that cannot move is defined. A
free joint of the manifest is an under-mated pair, and it is free.

So the input of a four-bar is a blue control: its loop moves every joint
of it. The spin of a saw blade is a red one.

No bpy.
"""

GROUND = "ground"
DEFINED = "defined"
FREE = "free"


def bridges(edges):
    """The keys of the edges that are bridges: edges on no cycle of the
    graph. `edges` is a list of (a, b, key), with a != b. An edge with the
    key None closes chains and is never reported. Two edges between the
    same two nodes are a cycle."""
    adj = {}
    for i, (a, b, _) in enumerate(edges):
        adj.setdefault(a, []).append((b, i))
        adj.setdefault(b, []).append((a, i))
    disc, low = {}, {}
    found = set()
    clock = 0
    for root in adj:
        if root in disc:
            continue
        disc[root] = low[root] = clock
        clock += 1
        # (node, the edge it was reached by, what is left of its edges);
        # a stack, not recursion, so a long chain cannot overflow
        stack = [(root, -1, iter(adj[root]))]
        while stack:
            v, came, rest = stack[-1]
            for w, i in rest:
                if i == came:
                    continue
                if w in disc:
                    low[v] = min(low[v], disc[w])
                    continue
                disc[w] = low[w] = clock
                clock += 1
                stack.append((w, i, iter(adj[w])))
                break
            else:
                stack.pop()
                if stack:
                    u = stack[-1][0]
                    low[u] = min(low[u], low[v])
                    if low[v] > disc[u]:
                        found.add(came)
    return {edges[i][2] for i in found if edges[i][2] is not None}


def tie(ends, other):
    """The two bodies a coupling ties: the far ends of its two joints from
    the body they share, or else the bodies the two joints move. `ends`
    and `other` are the (parent, child) bodies of the two joints."""
    (a1, b1), (a2, b2) = ends, other
    for s in (a1, b1):
        if s in (a2, b2):
            return (b1 if s == a1 else a1), (b2 if s == a2 else a2)
    return b1, b2


def classify(edges, ties, out):
    """Adds FREE or DEFINED to `out` for every keyed edge not in it yet.
    `ties` are the (body, body) pairs the couplings tie."""
    graph = list(edges) + [(a, b, None) for a, b in ties if a != b]
    free = bridges(graph)
    for _, _, key in edges:
        if key not in out:
            out[key] = FREE if key in free else DEFINED
    return out


def of_manifest(m):
    """{joint id: DEFINED or FREE} for the joints of a rig manifest."""
    grounded = {g.id for g in m.rigid_groups if g.grounded}

    def node(gid):
        return "" if gid in grounded else gid

    out = {}
    ends = {}
    edges = []
    for j in m.joints:
        if j.type == "free":
            out[j.id] = FREE
            continue
        a, b = node(j.parent_group), node(j.child_group)
        if a == b or j.type == "fixed":
            out[j.id] = DEFINED
        if a != b:
            ends[j.id] = (a, b)
            edges.append((a, b, j.id))
    ties = []
    for j in m.joints:
        c = j.coupling
        if c is None or not c.driver_joint or j.id not in ends:
            continue
        if c.driver_joint in ends:
            ties.append(tie(ends[j.id], ends[c.driver_joint]))
    return classify(edges, ties, out)
