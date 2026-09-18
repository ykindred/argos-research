def count_distinct(values):
    seen = []
    comparisons = 0
    for value in values:
        found = False
        for previous in seen:
            comparisons += 1
            if previous == value:
                found = True
                break
        if not found:
            seen.append(value)
    return len(seen), comparisons
