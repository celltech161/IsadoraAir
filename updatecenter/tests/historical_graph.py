"""Target lists for tests that wind the migration graph back to a HISTORICAL
node of one app (e.g. library at r0077 or r0092) while every other app stays
current.

An app whose migrations depend on a LATER migration of a pinned app cannot
stay at its leaf -- Django would have to unapply and re-apply it in one plan
(InvalidMigrationPlan), and the historical tree never contained it anyway.
Since 2.22C, spoken.0001 depends on library.0089. Such an app is held at its
latest own migration that fits the historical node, or treated as not yet
installed.
"""


def hold_dependents(graph, nodes, pinned: dict, *, drop_unapplied=False):
    """``nodes``: (app, name) targets, normally the leaves. ``pinned``:
    {app: historical migration name}. Returns the targets with every pinned
    app at its pinned node and every other app at its latest fitting node --
    or ``(app, None)`` (dropped instead when ``drop_unapplied``, for patched
    ``leaf_nodes``) when none of its migrations fits."""
    allowed = {app: {node for node in graph.forwards_plan((app, name)) if node[0] == app}
               for app, name in pinned.items()}

    def fits(node):
        return all(dep in allowed[dep[0]] for dep in graph.forwards_plan(node) if dep[0] in allowed)

    targets = []
    for app, name in nodes:
        if app in pinned:
            targets.append((app, pinned[app]))
            continue
        chain = [node for node in graph.forwards_plan((app, name)) if node[0] == app]
        fitting = [node for node in chain if fits(node)]
        if fitting:
            targets.append(fitting[-1])
        elif not drop_unapplied:
            targets.append((app, None))
    return targets
