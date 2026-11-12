import sys

from arete import factory

if __name__ == "__main__":
    fn, argv = factory.earlier(sys.argv[1:])
    factory.main(fn, argv)
