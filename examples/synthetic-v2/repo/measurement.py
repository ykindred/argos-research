"""Trusted fixture instrumentation; never use the candidate's returned counter."""


class Meter:
    def __init__(self):
        self.comparisons = 0
        self.hash_calls = 0

    def keys(self, values, collision=False):
        meter = self

        class Key:
            __slots__ = ("__value",)

            def __init__(self, value):
                self.__value = value

            def __eq__(self, other):
                meter.comparisons += 1
                return isinstance(other, Key) and self.__value == other.__value

            def __hash__(self):
                meter.hash_calls += 1
                return 0 if collision else hash(self.__value)

        return [Key(value) for value in values]
