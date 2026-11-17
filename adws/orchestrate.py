from arete import factory, orchestration


def orchestrate(flow):
    return orchestration.orchestrate(flow)


if __name__ == "__main__":
    factory.main(orchestrate)
