#!/usr/bin/env Rscript

# Calculate state-year population shares ages 17-19 and plot 2024 shares
# against state construction-employment percent changes from 2005 to 2010.

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
qcew_path <- if (length(args) >= 2) args[[2]] else default_path("processed_data", "qcew_state_naics2_quarterly_panel_1990_2025.parquet")
age_share_path <- if (length(args) >= 3) args[[3]] else default_path("processed_data", "state_age_17_19_population_share_2010_2024.parquet")
plot_data_path <- if (length(args) >= 4) args[[4]] else default_path("processed_data", "state_construction_pct_change_age_17_19_share_2024.parquet")
plot_path <- if (length(args) >= 5) args[[5]] else default_path("results", "state_construction_pct_change_age_17_19_share_2024.png")

construction_start_year <- 2005L
construction_end_year <- 2010L
age_share_start_year <- 2010L
age_share_end_year <- 2024L
plot_year <- 2024L
construction_naics <- "23"
target_ages <- 17:19
state_fips_51 <- c(
  "01", "02", "04", "05", "06", "08", "09", "10", "11", "12",
  "13", "15", "16", "17", "18", "19", "20", "21", "22", "23",
  "24", "25", "26", "27", "28", "29", "30", "31", "32", "33",
  "34", "35", "36", "37", "38", "39", "40", "41", "42", "44",
  "45", "46", "47", "48", "49", "50", "51", "53", "54", "55",
  "56"
)

state_abbrev <- data.table(
  state = c(state.name, "District of Columbia"),
  state_label = c(state.abb, "DC")
)

pep_single <- arrow::open_dataset(pep_path) %>%
  filter(
    state_fips %in% state_fips_51,
    age_granularity == "single_year",
    year >= age_share_start_year,
    year <= age_share_end_year
  ) %>%
  select(state_fips, state_name, year, age, population) %>%
  collect() %>%
  as.data.table()

age_shares <- pep_single[, .(
  population_17_19 = sum(population[age %in% target_ages], na.rm = TRUE),
  state_population = sum(population, na.rm = TRUE)
), by = .(state_fips, state = state_name, year)]
age_shares[, age_17_19_population_share := population_17_19 / state_population]
setorder(age_shares, state_fips, year)

expected_age_share_rows <- 51L * (age_share_end_year - age_share_start_year + 1L)
if (nrow(age_shares) != expected_age_share_rows) {
  stop("Expected ", expected_age_share_rows, " state-year age-share rows; observed ", nrow(age_shares))
}
if (any(!is.finite(age_shares$age_17_19_population_share))) {
  stop("Non-finite age 17-19 population shares detected")
}

dir.create(dirname(age_share_path), recursive = TRUE, showWarnings = FALSE)
arrow::write_parquet(as.data.frame(age_shares), age_share_path)

construction_annual <- arrow::open_dataset(qcew_path) %>%
  filter(
    state_fips %in% state_fips_51,
    naics2 == construction_naics,
    year %in% c(construction_start_year, construction_end_year)
  ) %>%
  select(state_fips, year, employment) %>%
  group_by(state_fips, year) %>%
  summarise(
    construction_employment = mean(employment, na.rm = TRUE),
    .groups = "drop"
  ) %>%
  collect() %>%
  as.data.table()

construction_wide <- dcast(
  construction_annual,
  state_fips ~ year,
  value.var = "construction_employment"
)
setnames(
  construction_wide,
  old = c(as.character(construction_start_year), as.character(construction_end_year)),
  new = c("construction_employment_2005", "construction_employment_2010")
)
construction_wide[, construction_employment_pct_change :=
  100 * (construction_employment_2010 / construction_employment_2005 - 1)]

age_share_2024 <- age_shares[year == plot_year, .(
  state_fips,
  state,
  population_17_19_2024 = population_17_19,
  state_population_2024 = state_population,
  age_17_19_population_share_2024 = age_17_19_population_share
)]

plot_df <- merge(construction_wide, age_share_2024, by = "state_fips", all = FALSE)
plot_df <- merge(plot_df, state_abbrev, by = "state", all.x = TRUE)
setcolorder(plot_df, c(
  "state_fips",
  "state",
  "state_label",
  "construction_employment_2005",
  "construction_employment_2010",
  "construction_employment_pct_change",
  "population_17_19_2024",
  "state_population_2024",
  "age_17_19_population_share_2024"
))
setorder(plot_df, state_fips)

if (nrow(plot_df) != 51L) {
  stop("Expected 51 states/DC in plot data; observed ", nrow(plot_df))
}
for (col in c("construction_employment_pct_change", "age_17_19_population_share_2024")) {
  if (any(!is.finite(plot_df[[col]]))) {
    stop("Non-finite values detected in ", col)
  }
}

dir.create(dirname(plot_data_path), recursive = TRUE, showWarnings = FALSE)
arrow::write_parquet(as.data.frame(plot_df), plot_data_path)

plot <- ggplot(
  plot_df,
  aes(
    x = construction_employment_pct_change,
    y = age_17_19_population_share_2024
  )
) +
  geom_vline(xintercept = 0, linewidth = 0.3, color = "gray70") +
  geom_smooth(method = "lm", formula = y ~ x, se = FALSE, color = "#d62728", linewidth = 0.8) +
  geom_point(color = "#1f77b4", size = 2.2, alpha = 0.85) +
  geom_text(
    aes(label = state_label),
    size = 2.6,
    vjust = -0.75,
    check_overlap = TRUE
  ) +
  scale_x_continuous(labels = function(x) paste0(x, "%")) +
  scale_y_continuous(labels = function(y) paste0(round(100 * y, 1), "%")) +
  labs(
    title = "State construction employment changes and age 17–19 population shares",
    subtitle = "Construction employment percent change is 2005–2010; age share is 2024",
    x = "Construction employment percent change, 2005–2010",
    y = "Population ages 17–19 as share of total population, 2024",
    caption = "Sources: BLS QCEW state NAICS-2 quarterly panel; Census PEP state age estimates."
  ) +
  theme_minimal(base_size = 11) +
  theme(
    plot.title = element_text(face = "bold"),
    panel.grid.minor = element_blank()
  )

dir.create(dirname(plot_path), recursive = TRUE, showWarnings = FALSE)
ggsave(plot_path, plot, width = 8, height = 6, dpi = 300)

cat("Wrote", age_share_path, "\n")
cat("Wrote", plot_data_path, "\n")
cat("Wrote", plot_path, "\n")
cat("Construction employment pct change range:",
    round(min(plot_df$construction_employment_pct_change), 2), "to",
    round(max(plot_df$construction_employment_pct_change), 2), "\n")
cat("2024 age 17-19 population share range:",
    round(min(plot_df$age_17_19_population_share_2024), 4), "to",
    round(max(plot_df$age_17_19_population_share_2024), 4), "\n")
cat("Correlation:",
    round(cor(plot_df$construction_employment_pct_change, plot_df$age_17_19_population_share_2024), 4), "\n")
