#!/usr/bin/env Rscript

# Plot all-industry shift-share exposure against the percent change in the
# number of state residents ages 15-19 from 2005 to 2024.
#
# The processed Census PEP file has 5-year age groups before 2010 and
# single-year ages for 2010 onward. Therefore, 2005 uses the PEP ages 15-19
# group, and 2024 sums exact single-year ages 15, 16, 17, 18, and 19 to match
# the 2005 definition.

suppressPackageStartupMessages({
  library(arrow)
  library(data.table)
  library(dplyr)
  library(ggplot2)
})

args <- commandArgs(trailingOnly = TRUE)

find_repo_root <- function() {
  raw_args <- commandArgs(trailingOnly = FALSE)
  script_arg <- raw_args[grepl("^--file=", raw_args)]
  starts <- getwd()
  if (length(script_arg) > 0) {
    script_path <- sub("^--file=", "", script_arg[[1]])
    starts <- c(
      starts,
      dirname(normalizePath(script_path, winslash = "/", mustWork = FALSE))
    )
  }

  for (start in unique(starts)) {
    current <- normalizePath(start, winslash = "/", mustWork = FALSE)
    repeat {
      if (file.exists(file.path(current, "AGENTS.MD")) &&
          dir.exists(file.path(current, "processed_data"))) {
        return(current)
      }
      parent <- dirname(current)
      if (identical(parent, current)) break
      current <- parent
    }
  }

  normalizePath(getwd(), winslash = "/", mustWork = FALSE)
}

repo_root <- find_repo_root()
default_path <- function(...) file.path(repo_root, ...)

pep_path <- if (length(args) >= 1) args[[1]] else default_path("processed_data", "census_pep_state_year_age_1990_2024.parquet")
shift_share_path <- if (length(args) >= 2) args[[2]] else default_path("processed_data", "state_shift_share_all_industries_2005_2010.parquet")
count_change_path <- if (length(args) >= 3) args[[3]] else default_path("processed_data", "state_age_15_19_count_change_2005_2024.parquet")
plot_data_path <- if (length(args) >= 4) args[[4]] else default_path("processed_data", "state_shift_share_all_industries_age_15_19_count_change_2005_2024.parquet")
plot_path <- if (length(args) >= 5) args[[5]] else default_path("results", "state_shift_share_all_industries_age_15_19_count_change_2005_2024.png")

if (!file.exists(shift_share_path)) {
  stop("Missing all-industry shift-share input: ", shift_share_path,
       ". Run scripts/calculate_shift_share_construction_2005_2010.R first.")
}

base_year <- 2005L
end_year <- 2024L
target_ages <- 15:19
state_fips_51 <- c(
  "01", "02", "04", "05", "06", "08", "09", "10", "11", "12",
  "13", "15", "16", "17", "18", "19", "20", "21", "22", "23",
  "24", "25", "26", "27", "28", "29", "30", "31", "32", "33",
  "34", "35", "36", "37", "38", "39", "40", "41", "42", "44",
  "45", "46", "47", "48", "49", "50", "51", "53", "54", "55",
  "56"
)

pep <- arrow::open_dataset(pep_path) %>%
  filter(
    state_fips %in% state_fips_51,
    year %in% c(base_year, end_year)
  ) %>%
  select(
    state_fips,
    state_name,
    year,
    age,
    age_start,
    age_end,
    age_granularity,
    population
  ) %>%
  collect() %>%
  as.data.table()

population_2005 <- pep[
  year == base_year &
    age_granularity == "age_group" &
    age_start == 15L &
    age_end == 19L,
  .(
    state = first(state_name),
    population_15_19_2005 = sum(population, na.rm = TRUE),
    population_15_19_2005_method = "PEP ages 15-19 group"
  ),
  by = state_fips
]

population_2024 <- pep[
  year == end_year &
    age_granularity == "single_year" &
    age %in% target_ages,
  .(
    population_15_19_2024 = sum(population, na.rm = TRUE),
    population_15_19_2024_method = "Summed exact PEP single-year ages 15, 16, 17, 18, and 19"
  ),
  by = state_fips
]

count_change <- merge(population_2005, population_2024, by = "state_fips", all = FALSE)
count_change[, age_15_19_population_pct_change_2005_2024 :=
  100 * (population_15_19_2024 / population_15_19_2005 - 1)]
setcolorder(count_change, c(
  "state_fips",
  "state",
  "population_15_19_2005",
  "population_15_19_2005_method",
  "population_15_19_2024",
  "population_15_19_2024_method",
  "age_15_19_population_pct_change_2005_2024"
))
setorder(count_change, state_fips)

if (nrow(count_change) != 51L) {
  stop("Expected 51 states/DC in count-change data; observed ", nrow(count_change))
}
if (any(!is.finite(count_change$age_15_19_population_pct_change_2005_2024))) {
  stop("Non-finite age 15-19 count changes detected")
}

shift_share <- arrow::open_dataset(shift_share_path) %>%
  select(
    state_fips,
    state_label,
    total_employment_2005,
    industry_employment_share_sum_2005,
    national_industry_employment_pct_change_2005_2010,
    shift_share_all_industries_2005_2010
  ) %>%
  collect() %>%
  as.data.table()

plot_df <- merge(count_change, shift_share, by = "state_fips", all = FALSE)
setcolorder(plot_df, c(
  "state_fips",
  "state",
  "state_label",
  "total_employment_2005",
  "industry_employment_share_sum_2005",
  "national_industry_employment_pct_change_2005_2010",
  "shift_share_all_industries_2005_2010",
  "population_15_19_2005",
  "population_15_19_2005_method",
  "population_15_19_2024",
  "population_15_19_2024_method",
  "age_15_19_population_pct_change_2005_2024"
))
setorder(plot_df, state_fips)

if (nrow(plot_df) != 51L) {
  stop("Expected 51 states/DC in plot data; observed ", nrow(plot_df))
}
for (col in c("shift_share_all_industries_2005_2010", "age_15_19_population_pct_change_2005_2024")) {
  if (any(!is.finite(plot_df[[col]]))) {
    stop("Non-finite values detected in ", col)
  }
}

write_parquet_dt <- function(dt, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  arrow::write_parquet(as.data.frame(dt), path)
}

write_parquet_dt(count_change, count_change_path)
write_parquet_dt(plot_df, plot_data_path)

plot <- ggplot(
  plot_df,
  aes(
    x = shift_share_all_industries_2005_2010,
    y = age_15_19_population_pct_change_2005_2024
  )
) +
  geom_hline(yintercept = 0, linewidth = 0.3, color = "gray70") +
  geom_vline(xintercept = 0, linewidth = 0.3, color = "gray70") +
  geom_smooth(method = "lm", formula = y ~ x, se = FALSE, color = "#d62728", linewidth = 0.8) +
  geom_point(color = "#1f77b4", size = 2.2, alpha = 0.85) +
  geom_text(
    aes(label = state_label),
    size = 2.6,
    vjust = -0.75,
    check_overlap = TRUE
  ) +
  scale_x_continuous(labels = function(x) paste0(round(x, 1), "%")) +
  scale_y_continuous(labels = function(y) paste0(y, "%")) +
  labs(
    title = "All-industry shift-share exposure and age 15–19 population changes",
    subtitle = "All-industry shift-share uses 2005 state NAICS-2 employment shares weighted by national industry employment growth, 2005–2010",
    x = "All-industry shift-share exposure (weighted national employment growth), 2005–2010",
    y = "Population ages 15–19 percent change, 2005–2024",
    caption = "All-industry shift-share uses BLS QCEW NAICS-2 employment; age counts use Census PEP state age estimates."
  ) +
  theme_minimal(base_size = 11) +
  theme(
    plot.title = element_text(face = "bold"),
    panel.grid.minor = element_blank()
  )

dir.create(dirname(plot_path), recursive = TRUE, showWarnings = FALSE)
ggsave(plot_path, plot, width = 8, height = 6, dpi = 300)

cat("Wrote", count_change_path, "\n")
cat("Wrote", plot_data_path, "\n")
cat("Wrote", plot_path, "\n")
cat("Age 15-19 population pct change range:",
    round(min(plot_df$age_15_19_population_pct_change_2005_2024), 2), "to",
    round(max(plot_df$age_15_19_population_pct_change_2005_2024), 2), "\n")
cat("Shift-share range:",
    round(min(plot_df$shift_share_all_industries_2005_2010), 4), "to",
    round(max(plot_df$shift_share_all_industries_2005_2010), 4), "\n")
cat("Correlation:",
    round(cor(plot_df$shift_share_all_industries_2005_2010, plot_df$age_15_19_population_pct_change_2005_2024), 4), "\n")
