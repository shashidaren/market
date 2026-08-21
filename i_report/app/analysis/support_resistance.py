import numpy as np
import pandas as pd


def find_pivots(df, window=3):
    highs = df["High"].values
    lows = df["Low"].values

    pivot_highs = []
    pivot_lows = []

    for i in range(window, len(df) - window):

        current_high = highs[i]
        current_low = lows[i]

        left_high = highs[i - window:i]
        right_high = highs[i + 1:i + window + 1]

        left_low = lows[i - window:i]
        right_low = lows[i + 1:i + window + 1]

        if (
            current_high >= max(left_high)
            and current_high >= max(right_high)
        ):
            pivot_highs.append(float(current_high))

        if (
            current_low <= min(left_low)
            and current_low <= min(right_low)
        ):
            pivot_lows.append(float(current_low))

    return pivot_lows, pivot_highs


def cluster_levels(levels, tolerance):
    """
    Group nearby pivot levels into price zones.

    Returns a list of dictionaries containing:
    - level
    - touches
    """

    if not levels:
        return []

    levels = sorted(levels)

    clusters = [[levels[0]]]

    for level in levels[1:]:

        current_cluster = clusters[-1]
        cluster_mean = np.mean(current_cluster)

        if abs(level - cluster_mean) <= tolerance:
            current_cluster.append(level)
        else:
            clusters.append([level])

    return [
        {
            "level": float(np.mean(cluster)),
            "touches": len(cluster)
        }
        for cluster in clusters
    ]


def get_nearest_below(zones, price, minimum_distance):
    """
    Find the nearest valid level below current price.
    """

    valid = [
        zone
        for zone in zones
        if zone["level"] < price - minimum_distance
    ]

    if not valid:
        return None

    return max(
        valid,
        key=lambda x: x["level"]
    )


def get_nearest_above(zones, price, minimum_distance):
    """
    Find the nearest valid level above current price.
    """

    valid = [
        zone
        for zone in zones
        if zone["level"] > price + minimum_distance
    ]

    if not valid:
        return None

    return min(
        valid,
        key=lambda x: x["level"]
    )


def calculate_levels(df):

    recent = df.tail(120).copy()

    price = float(
        recent["Close"].iloc[-1]
    )

    # --------------------------------------------------------
    # ATR CALCULATION
    # --------------------------------------------------------

    tr1 = recent["High"] - recent["Low"]

    tr2 = (
        recent["High"]
        - recent["Close"].shift()
    ).abs()

    tr3 = (
        recent["Low"]
        - recent["Close"].shift()
    ).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    atr = float(
        true_range.tail(14).mean()
    )

    average_daily_range = float(
        (
            recent["High"]
            - recent["Low"]
        ).tail(14).mean()
    )

    # --------------------------------------------------------
    # CLUSTER TOLERANCE
    # --------------------------------------------------------

    tolerance = max(
        atr * 0.75,
        price * 0.01
    )

    pivot_lows, pivot_highs = find_pivots(
        recent,
        window=3
    )

    # Add major range extremes

    pivot_lows.append(
        float(recent["Low"].min())
    )

    pivot_highs.append(
        float(recent["High"].max())
    )

    supports = cluster_levels(
        pivot_lows,
        tolerance
    )

    resistances = cluster_levels(
        pivot_highs,
        tolerance
    )

    # --------------------------------------------------------
    # LOCAL / TRADE LEVELS
    #
    # These can be closer to the current price and are useful
    # for stop-loss and short-term targets.
    # --------------------------------------------------------

    trade_minimum_distance = max(
        atr * 0.5,
        price * 0.005
    )

    trade_support = get_nearest_below(
        supports,
        price,
        trade_minimum_distance
    )

    trade_resistance = get_nearest_above(
        resistances,
        price,
        trade_minimum_distance
    )

    # --------------------------------------------------------
    # MAJOR / STRUCTURAL LEVELS
    #
    # These must be further away from current market noise.
    # --------------------------------------------------------

    major_minimum_distance = max(
        atr * 1.5,
        average_daily_range * 0.75,
        price * 0.015
    )

    major_support = get_nearest_below(
        supports,
        price,
        major_minimum_distance
    )

    major_resistance = get_nearest_above(
        resistances,
        price,
        major_minimum_distance
    )

    # --------------------------------------------------------
    # ALL VALID LEVELS
    # --------------------------------------------------------

    all_trade_supports = sorted(
        [
            zone
            for zone in supports
            if zone["level"] < price
        ],
        key=lambda x: x["level"],
        reverse=True
    )

    all_trade_resistances = sorted(
        [
            zone
            for zone in resistances
            if zone["level"] > price
        ],
        key=lambda x: x["level"]
    )

    # --------------------------------------------------------
    # RETURN RESULTS
    # --------------------------------------------------------

    return {

        "price": round(price, 4),

        # --------------------------------------------
        # LOCAL TRADE LEVELS
        # --------------------------------------------

        "trade_support": (
            round(
                trade_support["level"],
                4
            )
            if trade_support
            else None
        ),

        "trade_resistance": (
            round(
                trade_resistance["level"],
                4
            )
            if trade_resistance
            else None
        ),

        "trade_support_touches": (
            trade_support["touches"]
            if trade_support
            else 0
        ),

        "trade_resistance_touches": (
            trade_resistance["touches"]
            if trade_resistance
            else 0
        ),

        # --------------------------------------------
        # MAJOR STRUCTURAL LEVELS
        # --------------------------------------------

        "major_support": (
            round(
                major_support["level"],
                4
            )
            if major_support
            else None
        ),

        "major_resistance": (
            round(
                major_resistance["level"],
                4
            )
            if major_resistance
            else None
        ),

        "major_support_touches": (
            major_support["touches"]
            if major_support
            else 0
        ),

        "major_resistance_touches": (
            major_resistance["touches"]
            if major_resistance
            else 0
        ),

        # --------------------------------------------
        # BACKWARD COMPATIBILITY
        #
        # Existing code using support/resistance will
        # continue to receive major structural levels.
        # --------------------------------------------

        "support": (
            round(
                major_support["level"],
                4
            )
            if major_support
            else None
        ),

        "resistance": (
            round(
                major_resistance["level"],
                4
            )
            if major_resistance
            else None
        ),

        # --------------------------------------------
        # ALL LEVELS
        # --------------------------------------------

        "all_supports": [
            {
                "level": round(
                    x["level"],
                    4
                ),
                "touches": x["touches"]
            }
            for x in all_trade_supports
        ],

        "all_resistances": [
            {
                "level": round(
                    x["level"],
                    4
                ),
                "touches": x["touches"]
            }
            for x in all_trade_resistances
        ],

        # --------------------------------------------
        # VOLATILITY DATA
        # --------------------------------------------

        "atr": round(
            atr,
            4
        ),

        "average_daily_range": round(
            average_daily_range,
            4
        ),

        "trade_minimum_distance": round(
            trade_minimum_distance,
            4
        ),

        "major_minimum_distance": round(
            major_minimum_distance,
            4
        )
    }
