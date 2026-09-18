from algorithm import count_distinct


def check():
    for values in [[], [1], [1, 1], [-1, 0, -1], list(range(100)), [3, 2] * 50]:
        assert count_distinct(values)[0] == len(set(values))


if __name__ == "__main__":
    check()
