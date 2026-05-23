SELECT
    year,
    quarter,
    location_name,
    SUM(COALESCE(net_carpet_area_sq_m, 0)) AS total_carpet_area_sq_m,
    COUNT(*) AS transaction_count
FROM transactions
WHERE
    agreement_price >= 1
    AND year BETWEEN 2021 AND 2024
    AND transaction_category IN ('Sale')
    AND project_type IN ('Residential')
    AND (
        location_name ILIKE '%Baner%'
        OR village_name ILIKE '%Baner%'
        OR location_name ILIKE '%Balewadi%'
        OR village_name ILIKE '%Balewadi%'
    )
GROUP BY
    year,
    quarter,
    location_name;
