from algorithm import count_distinct
from measurement import Meter


def check():
    for values in [[], [1], [1, 1], [-1, 0, -1], list(range(100)), [3, 2] * 50]:
        expected = len(set(values))
        assert count_distinct(values)[0] == expected
        for collision in (False, True):
            assert count_distinct(Meter().keys(values, collision))[0] == expected


if __name__ == "__main__":
    check()
