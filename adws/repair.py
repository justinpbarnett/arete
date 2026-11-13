from feature import built

from arete import factory


def repair(flow):
    return built(flow, [("repair", {"tree": str(flow.ctx.ws)})])


if __name__ == "__main__":
    factory.main(repair)
