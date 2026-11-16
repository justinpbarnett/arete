from feature import start

from arete import factory


def patch(flow):
    return start(flow, request_patch=True)


if __name__ == "__main__":
    factory.main(patch)
