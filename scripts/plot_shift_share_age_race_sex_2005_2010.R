#!/usr/bin/env Rscript

# Plot the ACS age-race-sex employment shift-share exposure against fertility
# changes, 2024 age 17-19 population shares, and age 15-19 count changes.

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

shift_share_path <- if (length(args) >= 1) args[[1]] else default_path("processed_data", "state_shift_share_age_race_sex_2005_2010.parquet")
tfr_path <- if (length(args) >= 2) args[[2]] else default_path("processed_data", "tfr_state_year_1995_2024.parquet")
pep_path <- if (length(args) >= 3) args[[3]] else default_path("processed_data", "census_pep_state_year_age_1990_2024.parquet")
tfr_plot_data_path <- if (length(args) >= 4) args[[4]] else default_path("processed_data", "state_shift_share_age_race_sex_tfr_pct_changes_2005_2010.parquet")
age_share_plot_data_path <- if (length(args) >= 5) args[[5]] else default_path("processed_data", "state_shift_share_age_race_sex_age_17_19_share_2024.parquet")
age_count_plot_data_path <- if (length(args) >= 6) args[[6]] else default_path("processed_data", "state_shift_share_age_race_sex_age_15_19_count_change_2005_2024.parquet")
tfr_plot_path <- if (length(args) >= 7) args[[7]] else default_path("results", "state_shift_share_age_race_sex_tfr_pct_changes_2005_2010.png")
age_share_plot_path <- if (length(args) >= 8) args[[8]] else default_path("results", "state_shift_share_age_race_sex_age_17_19_share_2024.png")
age_count_plot_path <- if (length(args) >= 9) args[[9]] else default_path("results", "state_shift_share_age_race_sex_age_15_19_count_change_2005_2024.png")

if (!file.exists(shift_share_path)) {
  stop("Missing age-race-sex shift-share input: ", shift_share_path,
       ". Run scripts/calculate_shift_share_age_race_sex_2005_2010.py first.")
}

start_year <- 2005L
end_year <- 2010L
age_share_year <- 2024L
age_count_end_year <- 2024L
target_age_share_ages <- 17:19
target_count_ages <- 15:19
state_fips_51 <- c(
  "01", "02", "04", "05", "06", "08", "09", "10", "11", "12",
  "13", "15", "16", "17", "18", "19", "20", "21", "22", "23",
  "24", "25", "26", "27", "28", "29", "30", "31", "32", "33",
  "34", "35", "36", "37", "38", "39", "40", "41", "42", "44",
  "45", "46", "47", "48", "49", "50", "51", "53", "54", "55",
  "56"
)

shift_share <- arrow::open_dataset(shift_share_path) %>%
  select(
    state_fips,
    state,
    state_label,
    total_age_race_sex_employment_2005,
    age_race_sex_employment_share_sum_2005,
    national_age_race_sex_employment_pct_change_2005_2010,
    shift_share_age_race_sex_2005_2010
  ) %>%
  collect() %>%
  as.data.table()

if (nrow(shift_share) != 51L) {
  stop("Expected 51 states/DC in shift-share input; observed ", nrow(shift_share))
}
if (any(!is.finite(shift_share$shift_share_age_race_sex_2005_2010))) {
  stop("Non-finite age-race-sex shift-share values detected")
}

# TFR percent changes, 2005-2010.
tfr_wide <- arrow::open_dataset(tfr_path) %>%
  filter(year %in% c(start_year, end_year)) %>%
  select(state_code, year, tfr) %>%
  collect() %>%
  as.data.table() %>%
  dcast(state_code ~ year, value.var = "tfr")
setnames(
  tfr_wide,
  old = c("state_code", as.character(start_year), as.character(end_year)),
  new = c("state_fips", "tfr_2005", "tfr_2010")
)
tfr_wide[, `:=`(
  tfr_pct_change = 100 * (tfr_2010 / tfr_2005 - 1),
  tfr_level_change = tfr_2010 - tfr_2005,
  fertility_rate_per_1000_change = 1000 * (tfr_2010 - tfr_2005)
)]

tfr_plot_df <- merge(shift_share, tfr_wide, by = "state_fips", all = FALSE)
setcolorder(tfr_plot_df, c(
  "state_fips",
  "state",
  "state_label",
  "total_age_race_sex_employment_2005",
  "age_race_sex_employment_share_sum_2005",
  "national_age_race_sex_employment_pct_change_2005_2010",
  "shift_share_age_race_sex_2005_2010",
  "tfr_2005",
  "tfr_2010",
  "tfr_pct_change",
  "tfr_level_change",
  "fertility_rate_per_1000_change"
))
setorder(tfr_plot_df, state_fips)

# 2024 age 17-19 population share and 2005-2024 age 15-19 population count change.
pep <- arrow::open_dataset(pep_path) %>%
  filter(
    state_fips %in% state_fips_51,
    year %in% c(start_year, age_share_year)
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

age_share_2024 <- pep[
  year == age_share_year & age_granularity == "single_year",
  .(
    population_17_19_2024 = sum(population[age %in% target_age_share_ages], na.rm = TRUE),
    state_population_2024 = sum(population, na.rm = TRUE)
  ),
  by = state_fips
]
age_share_2024[, age_17_19_population_share_2024 :=
  population_17_19_2024 / state_population_2024]

age_share_plot_df <- merge(shift_share, age_share_2024, by = "state_fips", all = FALSE)
setcolorder(age_share_plot_df, c(
  "state_fips",
  "state",
  "state_label",
  "total_age_race_sex_employment_2005",
  "age_race_sex_employment_share_sum_2005",
  "national_age_race_sex_employment_pct_change_2005_2010",
  "shift_share_age_race_sex_2005_2010",
  "population_17_19_2024",
  "state_population_2024",
  "age_17_19_population_share_2024"
))
setorder(age_share_plot_df, state_fips)

population_2005 <- pep[
  year == start_year &
    age_granularity == "age_group" &
    age_start == 15L &
    age_end == 19L,
  .(
    population_15_19_2005 = sum(population, na.rm = TRUE),
    population_15_19_2005_method = "PEP ages 15-19 group"
  ),
  by = state_fips
]

population_2024 <- pep[
  year == age_count_end_year &
    age_granularity == "single_year" &
    age %in% target_count_ages,
  .(
    population_15_19_2024 = sum(population, na.rm = TRUE),
    population_15_19_2024_method = "Summed exact PEP single-year ages 15, 16, 17, 18, and 19"
  ),
  by = state_fips
]

count_change <- merge(population_2005, population_2024, by = "state_fips", all = FALSE)
count_change[, age_15_19_population_pct_change_2005_2024 :=
  100 * (population_15_19_2024 / population_15_19_2005 - 1)]

age_count_plot_df <- merge(shift_share, count_change, by = "state_fips", all = FALSE)
setcolorder(age_count_plot_df, c(
  "state_fips",
  "state",
  "state_label",
  "total_age_race_sex_employment_2005",
  "age_race_sex_employment_share_sum_2005",
  "national_age_race_sex_employment_pct_change_2005_2010",
  "shift_share_age_race_sex_2005_2010",
  "population_15_19_2005",
  "population_15_19_2005_method",
  "population_15_19_2024",
  "population_15_19_2024_method",
  "age_15_19_population_pct_change_2005_2024"
))
setorder(age_count_plot_df, state_fips)

for (dt_name in c("tfr_plot_df", "age_share_plot_df", "age_count_plot_df")) {
  dt <- get(dt_name)
  if (nrow(dt) != 51L) {
    stop("Expected 51 states/DC in ", dt_name, "; observed ", nrow(dt))
  }
  if (any(!is.finite(dt$shift_share_age_race_sex_2005_2010))) {
    stop("Non-finite shift-share values detected in ", dt_name)
  }
}
if (any(!is.finite(tfr_plot_df$tfr_pct_change))) {
  stop("Non-finite TFR percent changes detected")
}
if (any(!is.finite(age_share_plot_df$age_17_19_population_share_2024))) {
  stop("Non-finite 2024 age-share values detected")
}
if (any(!is.finite(age_count_plot_df$age_15_19_population_pct_change_2005_2024))) {
  stop("Non-finite age 15-19 count changes detected")
}

write_parquet_dt <- function(dt, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  arrow::write_parquet(as.data.frame(dt), path)
}

write_parquet_dt(tfr_plot_df, tfr_plot_data_path)
write_parquet_dt(age_share_plot_df, age_share_plot_data_path)
write_parquet_dt(age_count_plot_df, age_count_plot_data_path)

base_caption_prefix <- paste0(
  "Age-race-sex shift-share uses ACS 1-year B23002A-G employment bins ",
  "(race-alone/two-or-more categories) and treats missing fine-cell estimates as zero."
)

base_plot <- function(data, y_var, y_label, title, subtitle, caption_suffix) {
  ggplot(
    data,
    aes(
      x = shift_share_age_race_sex_2005_2010,
      y = .data[[y_var]]
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
    labs(
      title = title,
      subtitle = subtitle,
      x = "Age-race-sex shift-share exposure (weighted national employment growth), 2005–2010",
      y = y_label,
      caption = paste(base_caption_prefix, caption_suffix, sep = " ")
    ) +
    theme_minimal(base_size = 11) +
    theme(
      plot.title = element_text(face = "bold"),
      panel.grid.minor = element_blank()
    )
}

subtitle_text <- paste0(
  "Shift-share = 2005 state employment shares across ACS age-race-sex bins × ",
  "national bin employment growth, 2005–2010"
)

tfr_plot <- base_plot(
  tfr_plot_df,
  "tfr_pct_change",
  "TFR percent change, 2005–2010",
  "Age-race-sex shift-share exposure and fertility changes",
  subtitle_text,
  "Fertility data use CDC WONDER births and population denominators."
) +
  scale_y_continuous(labels = function(y) paste0(y, "%"))

age_share_plot <- base_plot(
  age_share_plot_df,
  "age_17_19_population_share_2024",
  "Population ages 17–19 as share of total population, 2024",
  "Age-race-sex shift-share exposure and age 17–19 population shares",
  subtitle_text,
  "Age shares use Census PEP state age estimates."
) +
  scale_y_continuous(labels = function(y) paste0(round(100 * y, 1), "%"))

age_count_plot <- base_plot(
  age_count_plot_df,
  "age_15_19_population_pct_change_2005_2024",
  "Population ages 15–19 percent change, 2005–2024",
  "Age-race-sex shift-share exposure and age 15–19 population changes",
  subtitle_text,
  "Age counts use Census PEP state age estimates."
) +
  scale_y_continuous(labels = function(y) paste0(y, "%"))

dir.create(dirname(tfr_plot_path), recursive = TRUE, showWarnings = FALSE)
ggsave(tfr_plot_path, tfr_plot, width = 8, height = 6, dpi = 300)
ggsave(age_share_plot_path, age_share_plot, width = 8, height = 6, dpi = 300)
ggsave(age_count_plot_path, age_count_plot, width = 8, height = 6, dpi = 300)

cat("Wrote", tfr_plot_data_path, "\n")
cat("Wrote", age_share_plot_data_path, "\n")
cat("Wrote", age_count_plot_data_path, "\n")
cat("Wrote", tfr_plot_path, "\n")
cat("Wrote", age_share_plot_path, "\n")
cat("Wrote", age_count_plot_path, "\n")
cat("Age-race-sex shift-share range:",
    round(min(shift_share$shift_share_age_race_sex_2005_2010), 4), "to",
    round(max(shift_share$shift_share_age_race_sex_2005_2010), 4), "\n")
cat("TFR plot correlation:",
    round(cor(tfr_plot_df$shift_share_age_race_sex_2005_2010, tfr_plot_df$tfr_pct_change), 4), "\n")
cat("Age-share plot correlation:",
    round(cor(age_share_plot_df$shift_share_age_race_sex_2005_2010, age_share_plot_df$age_17_19_population_share_2024), 4), "\n")
cat("Age 15-19 count-change plot correlation:",
    round(cor(age_count_plot_df$shift_share_age_race_sex_2005_2010, age_count_plot_df$age_15_19_population_pct_change_2005_2024), 4), "\n")
