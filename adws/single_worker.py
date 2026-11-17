from arete import factory, orchestration


def single_worker(flow):
    return orchestration.single_worker(flow)


if __name__ == "__main__":
    factory.main(single_worker)
