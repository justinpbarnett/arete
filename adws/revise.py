from feature import built

from arete import factory


def revise(flow):
    return built(flow, [("revise", {"tree": str(flow.ctx.ws)})])


if __name__ == "__main__":
    factory.main(revise)
