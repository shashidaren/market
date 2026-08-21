WEIGHTS = {
    "technical": 0.30,
    "volume": 0.15,
    "fundamental": 0.20,
    "sentiment": 0.10,
    "institutional": 0.10,
    "risk": 0.15,
}


def calculate_final_score(results):

    weighted_score = 0
    total_weight = 0

    for name, result in results.items():

        if result is None:
            continue

        weight = WEIGHTS.get(name, 0)

        weighted_score += (
            result.score * weight
        )

        total_weight += weight

    if total_weight == 0:
        return 0

    return round(
        weighted_score / total_weight,
        2
    )
