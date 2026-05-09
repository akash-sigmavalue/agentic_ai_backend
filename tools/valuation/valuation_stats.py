"""
Valuation Stats — Statistical engine for property valuation.
Handles confidence intervals, pooled variance, and other probabilistic metrics.
"""

import logging
import numpy as np
from scipy import stats
from typing import Tuple, List, Optional, Dict

logger = logging.getLogger("valuation_stats")

def calculate_project_ci(
    rates: List[float], 
    confidence_level: float = 0.90
) -> Tuple[float, float, float]:
    """
    Calculates the Confidence Interval for a project using Student's T-distribution.
    """
    n = len(rates)
    avg_rate = float(np.mean(rates))

    if n < 2:
        return avg_rate, avg_rate, 0.0

    df = n - 1
    std_dev = float(np.std(rates, ddof=1))
    sem = std_dev / np.sqrt(n)

    t_crit = stats.t.ppf((1 + confidence_level) / 2, df)
    margin_error = float(t_crit * sem)
    
    lower_bound = float(round(avg_rate - margin_error, 2))
    upper_bound = float(round(avg_rate + margin_error, 2))
    
    return lower_bound, upper_bound, round(margin_error, 2)

def calculate_pooled_variance_ci(
    project_groups: List[List[float]], 
    confidence_level: float = 0.90
) -> Tuple[float, float, float]:
    """
    Calculates pooled variance across multiple projects.
    """
    all_rates = [r for group in project_groups for r in group]
    return calculate_project_ci(all_rates, confidence_level)

def compute_final_valuation(rates: List[float]) -> Dict:
    """
    Computes a comprehensive valuation result for the subject property 
    based on a pool of relevant rates.
    """
    if not rates:
        return {
            "mean_rate": 0,
            "std_dev": 0,
            "sample_size": 0,
            "sem": 0,
            "moe": {"90": 0, "95": 0, "99": 0},
            "critical_values": {"90": 0, "95": 0, "99": 0}
        }

    n_val = len(rates)
    mean_rate = float(np.mean(rates))
    std_dev = float(np.std(rates, ddof=1)) if n_val > 1 else 0.0
    sem = std_dev / np.sqrt(n_val) if n_val > 0 else 0.0

    moe = {}
    critical_values = {}
    
    levels = {"90": 0.90, "95": 0.95, "99": 0.99}
    df = max(1, n_val - 1)

    for key, level in levels.items():
        if n_val >= 2:
            t_crit = stats.t.ppf((1 + level) / 2, df)
            m = t_crit * sem
        else:
            t_crit = 0.0
            m = 0.0
        
        moe[key] = round(m, 2)
        critical_values[key] = round(t_crit, 4)

    return {
        "mean_rate": round(mean_rate, 2),
        "std_dev": round(std_dev, 2),
        "sample_size": n_val,
        "sem": round(sem, 2),
        "moe": moe,
        "critical_values": critical_values
    }
