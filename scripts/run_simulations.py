#!/usr/bin/env python3

import argparse
import random
import statistics


COX_WEIGHTS = {
    "Dexterous prayer scroll": 20,
    "Arcane prayer scroll": 20,
    "Twisted buckler": 4,
    "Dragon hunter crossbow": 4,
    "Dinh's bulwark": 3,
    "Ancestral hat": 3,
    "Ancestral robe top": 3,
    "Ancestral robe bottom": 3,
    "Dragon claws": 3,
    "Elder maul": 2,
    "Kodai insignia": 2,
    "Twisted bow": 2,
}


def unique_prob_with_reroll(weights, target, reroll_prob):
    """
    Probability of getting `target`, conditional on already getting a unique,
    allowing a re-roll with probability `reroll_prob`.

    Re-roll rule:
      - Roll once on the unique table.
      - If the first item is not the target, then with probability `reroll_prob`,
        re-roll with the first item removed from the table.
    """

    if target not in weights:
        raise ValueError(f"{target} is not in the unique table.")

    total_weight = sum(weights.values())
    target_weight = weights[target]

    # Chance of getting target on the first unique roll
    prob = target_weight / total_weight

    # Additional chance from re-rolling every non-target unique
    for item, weight in weights.items():
        if item == target:
            continue

        prob_initial_item = weight / total_weight
        reroll_total_weight = total_weight - weight
        prob_target_on_reroll = target_weight / reroll_total_weight

        prob += (
            prob_initial_item
            * reroll_prob
            * prob_target_on_reroll
        )

    return prob


def weighted_roll(weights, rng, removed_item=None):
    """
    Roll one item from the weighted unique table.

    If removed_item is provided, that item is excluded from the roll.
    """

    eligible_items = {
        item: weight
        for item, weight in weights.items()
        if item != removed_item
    }

    total_weight = sum(eligible_items.values())
    roll = rng.uniform(0, total_weight)

    cumulative = 0
    for item, weight in eligible_items.items():
        cumulative += weight
        if roll <= cumulative:
            return item

    # Fallback for floating-point edge cases
    return item


def simulate_until_tbow(weights, unique_prob, use_reroll, reroll_prob, rng):
    """
    Simulate number of CoX completions until Twisted bow.
    """

    raids = 0

    while True:
        raids += 1

        # No purple this raid
        if rng.random() > unique_prob:
            continue

        first_item = weighted_roll(weights, rng)

        if first_item == "Twisted bow":
            return raids

        if use_reroll and rng.random() <= reroll_prob:
            second_item = weighted_roll(
                weights=weights,
                rng=rng,
                removed_item=first_item,
            )

            if second_item == "Twisted bow":
                return raids


def simulate_until_all_uniques(weights, unique_prob, use_reroll, reroll_prob, rng):
    """
    Simulate number of CoX completions until all uniques have been received.

    Collection-log re-roll rule:
      - If the first unique is new, keep it.
      - If the first unique is a duplicate, then with probability `reroll_prob`,
        re-roll it with that duplicate item removed from the table.
      - The re-roll result is kept, whether it is new or another duplicate.
    """

    raids = 0
    collected = set()
    all_uniques = set(weights.keys())

    while collected != all_uniques:
        raids += 1

        # No purple this raid
        if rng.random() > unique_prob:
            continue

        first_item = weighted_roll(weights, rng)

        if first_item not in collected:
            collected.add(first_item)
            continue

        if use_reroll and rng.random() <= reroll_prob:
            second_item = weighted_roll(
                weights=weights,
                rng=rng,
                removed_item=first_item,
            )
            collected.add(second_item)

    return raids


def summarize(values):
    return {
        "median": statistics.median(values),
        "average": statistics.mean(values),
    }


def run_simulations(weights, unique_prob, reroll_prob, trials, seed):
    rng = random.Random(seed)

    results = {}

    scenarios = [
        ("Tbow", False),
        ("Tbow", True),
        ("All uniques", False),
        ("All uniques", True),
    ]

    for goal, use_reroll in scenarios:
        sims = []

        for _ in range(trials):
            if goal == "Tbow":
                raids = simulate_until_tbow(
                    weights=weights,
                    unique_prob=unique_prob,
                    use_reroll=use_reroll,
                    reroll_prob=reroll_prob,
                    rng=rng,
                )
            else:
                raids = simulate_until_all_uniques(
                    weights=weights,
                    unique_prob=unique_prob,
                    use_reroll=use_reroll,
                    reroll_prob=reroll_prob,
                    rng=rng,
                )

            sims.append(raids)

        results[(goal, use_reroll)] = summarize(sims)

    return results


def print_results(results, unique_rate_x, reroll_rate_x, trials, reroll_prob):
    print()
    print("CoX simulation results")
    print(f"Trials: {trials:,}")
    print(f"Unique probability per completion: 1/{unique_rate_x:g}")
    print(f"Re-roll success probability:        1/{reroll_rate_x:g}")
    print()

    header = (
        f"{'Goal':<15} "
        f"{'Mechanic':<15} "
        f"{'Median raids':>15} "
        f"{'Average raids':>15}"
    )
    print(header)
    print("-" * len(header))

    for goal, use_reroll in [
        ("Tbow", False),
        ("Tbow", True),
        ("All uniques", False),
        ("All uniques", True),
    ]:
        mechanic = "With re-roll" if use_reroll else "No re-roll"
        median_raids = results[(goal, use_reroll)]["median"]
        average_raids = results[(goal, use_reroll)]["average"]

        print(
            f"{goal:<15} "
            f"{mechanic:<15} "
            f"{median_raids:>15,.0f} "
            f"{average_raids:>15,.1f}"
        )

    print()

    tbow_conditional_no_reroll = COX_WEIGHTS["Twisted bow"] / sum(COX_WEIGHTS.values())
    tbow_conditional_with_reroll = unique_prob_with_reroll(
        weights=COX_WEIGHTS,
        target="Twisted bow",
        reroll_prob=reroll_prob,
    )

    print("Conditional-on-purple Twisted bow rates:")
    print(f"  No re-roll:   {tbow_conditional_no_reroll:.4%}")
    print(f"  With re-roll: {tbow_conditional_with_reroll:.4%}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Simulate CoX completions until Tbow and until all uniques."
    )

    parser.add_argument(
        "--unique-rate-x",
        type=float,
        required=True,
        help=(
            "Chance of getting a unique per CoX completion is 1/x. "
            "Example: 27 means 1/27."
        ),
    )

    parser.add_argument(
        "--reroll-rate-x",
        type=float,
        required=True,
        help=(
            "Chance that the re-roll succeeds/activates is 1/x. "
            "Example: 5 means 1/5. Use 1 for guaranteed re-roll."
        ),
    )

    parser.add_argument(
        "--trials",
        type=int,
        default=100_000,
        help="Number of simulation trials. Default: 100,000.",
    )

    parser.add_argument(
        "--seed",
        type=int,
        default=12345,
        help="Random seed. Default: 12345.",
    )

    args = parser.parse_args()

    if args.unique_rate_x <= 0:
        raise ValueError("--unique-rate-x must be positive.")

    if args.reroll_rate_x <= 0:
        raise ValueError("--reroll-rate-x must be positive.")

    unique_prob = 1 / args.unique_rate_x
    reroll_prob = 1 / args.reroll_rate_x

    if unique_prob > 1:
        raise ValueError("--unique-rate-x implies a probability greater than 1.")

    if reroll_prob > 1:
        raise ValueError("--reroll-rate-x implies a probability greater than 1.")

    results = run_simulations(
        weights=COX_WEIGHTS,
        unique_prob=unique_prob,
        reroll_prob=reroll_prob,
        trials=args.trials,
        seed=args.seed,
    )

    print_results(
        results=results,
        unique_rate_x=args.unique_rate_x,
        reroll_rate_x=args.reroll_rate_x,
        trials=args.trials,
        reroll_prob=reroll_prob,
    )


if __name__ == "__main__":
    main()