#!/usr/bin/env Rscript

# Plot all-industry shift-share exposure against fertility changes and 2024
# age 17-19 population shares.

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

shift_share_path <- if (length(args) >= 1) args[[1]] else default_path("processed_data", "state_shift_share_all_industries_2005_2010.parquet")
tfr_path <- if (length(args) >= 2) args[[2]] else default_path("processed_data", "tfr_state_year_1995_2024.parquet")
age_share_path <- if (length(args) >= 3) args[[3]] else default_path("processed_data", "state_age_17_19_population_share_2010_2024.parquet")
tfr_plot_data_path <- if (length(args) >= 4) args[[4]] else default_path("processed_data", "state_shift_share_all_industries_tfr_pct_changes_2005_2010.parquet")
age_plot_data_path <- if (length(args) >= 5) args[[5]] else default_path("processed_data", "state_shift_share_all_industries_age_17_19_share_2024.parquet")
tfr_plot_path <- if (length(args) >= 6) args[[6]] else default_path("results", "state_shift_share_all_industries_tfr_pct_changes_2005_2010.png")
age_plot_path <- if (length(args) >= 7) args[[7]] else default_path("results", "state_shift_share_all_industries_age_17_19_share_2024.png")

if (!file.exists(shift_share_path)) {
  stop("Missing all-industry shift-share input: ", shift_share_path,
       ". Run scripts/calculate_shift_share_construction_2005_2010.R first.")
}
if (!file.exists(age_share_path)) {
  stop("Missing age-share input: ", age_share_path,
       ". Run scripts/plot_age_17_19_share_tfr_changes_2005_2010.R first.")
}

start_year <- 2005L
end_year <- 2010L
plot_year <- 2024L

shift_share <- arrow::open_dataset(shift_share_path) %>%
  select(
    state_fips,
    state,
    state_label,
    total_employment_2005,
    industry_employment_share_sum_2005,
    national_industry_employment_pct_change_2005_2010,
    shift_share_all_industries_2005_2010
  ) %>%
  collect() %>%
  as.data.table()

if (nrow(shift_share) != 51L) {
  stop("Expected 51 states/DC in shift-share input; observed ", nrow(shift_share))
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
  "total_employment_2005",
  "industry_employment_share_sum_2005",
  "national_industry_employment_pct_change_2005_2010",
  "shift_share_all_industries_2005_2010",
  "tfr_2005",
  "tfr_2010",
  "tfr_pct_change",
  "tfr_level_change",
  "fertility_rate_per_1000_change"
))
setorder(tfr_plot_df, state_fips)

# 2024 age 17-19 population share.
age_share_2024 <- arrow::open_dataset(age_share_path) %>%
  filter(year == plot_year) %>%
  select(
    state_fips,
    population_17_19,
    state_population,
    age_17_19_population_share
  ) %>%
  collect() %>%
  as.data.table()
setnames(age_share_2024, old = c(
  "population_17_19",
  "state_population",
  "age_17_19_population_share"
), new = c(
  "population_17_19_2024",
  "state_population_2024",
  "age_17_19_population_share_2024"
))

age_plot_df <- merge(shift_share, age_share_2024, by = "state_fips", all = FALSE)
setcolorder(age_plot_df, c(
  "state_fips",
  "state",
  "state_label",
  "total_employment_2005",
  "industry_employment_share_sum_2005",
  "national_industry_employment_pct_change_2005_2010",
  "shift_share_all_industries_2005_2010",
  "population_17_19_2024",
  "state_population_2024",
  "age_17_19_population_share_2024"
))
setorder(age_plot_df, state_fips)

for (dt_name in c("tfr_plot_df", "age_plot_df")) {
  dt <- get(dt_name)
  if (nrow(dt) != 51L) {
    stop("Expected 51 states/DC in ", dt_name, "; observed ", nrow(dt))
  }
  if (any(!is.finite(dt$shift_share_all_industries_2005_2010))) {
    stop("Non-finite shift-share values detected in ", dt_name)
  }
}
if (any(!is.finite(tfr_plot_df$tfr_pct_change))) {
  stop("Non-finite TFR percent changes detected")
}
if (any(!is.finite(age_plot_df$age_17_19_population_share_2024))) {
  stop("Non-finite 2024 age-share values detected")
}

write_parquet_dt <- function(dt, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  arrow::write_parquet(as.data.frame(dt), path)
}

write_parquet_dt(tfr_plot_df, tfr_plot_data_path)
write_parquet_dt(age_plot_df, age_plot_data_path)

base_plot <- function(data, y_var, y_label, title, subtitle, caption) {
  ggplot(
    data,
    aes(
      x = shift_share_all_industries_2005_2010,
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
      x = "All-industry shift-share exposure (weighted national employment growth), 2005–2010",
      y = y_label,
      caption = caption
    ) +
    theme_minimal(base_size = 11) +
    theme(
      plot.title = element_text(face = "bold"),
      panel.grid.minor = element_blank()
    )
}

tfr_plot <- base_plot(
  tfr_plot_df,
  "tfr_pct_change",
  "TFR percent change, 2005–2010",
  "All-industry shift-share exposure and fertility changes",
  "All-industry shift-share uses 2005 state NAICS-2 employment shares weighted by national industry employment growth, 2005–2010",
  "All-industry shift-share uses BLS QCEW NAICS-2 employment; fertility data use CDC WONDER births and population denominators."
) +
  scale_y_continuous(labels = function(y) paste0(y, "%"))

age_plot <- base_plot(
  age_plot_df,
  "age_17_19_population_share_2024",
  "Population ages 17–19 as share of total population, 2024",
  "All-industry shift-share exposure and age 17–19 population shares",
  "All-industry shift-share uses 2005 state NAICS-2 employment shares weighted by national industry employment growth, 2005–2010",
  "All-industry shift-share uses BLS QCEW NAICS-2 employment; age shares use Census PEP state age estimates."
) +
  scale_y_continuous(labels = function(y) paste0(round(100 * y, 1), "%"))

dir.create(dirname(tfr_plot_path), recursive = TRUE, showWarnings = FALSE)
ggsave(tfr_plot_path, tfr_plot, width = 8, height = 6, dpi = 300)
ggsave(age_plot_path, age_plot, width = 8, height = 6, dpi = 300)

cat("Wrote", tfr_plot_data_path, "\n")
cat("Wrote", age_plot_data_path, "\n")
cat("Wrote", tfr_plot_path, "\n")
cat("Wrote", age_plot_path, "\n")
cat("Shift-share range:",
    round(min(shift_share$shift_share_all_industries_2005_2010), 4), "to",
    round(max(shift_share$shift_share_all_industries_2005_2010), 4), "\n")
cat("TFR plot correlation:",
    round(cor(tfr_plot_df$shift_share_all_industries_2005_2010, tfr_plot_df$tfr_pct_change), 4), "\n")
cat("Age-share plot correlation:",
    round(cor(age_plot_df$shift_share_all_industries_2005_2010, age_plot_df$age_17_19_population_share_2024), 4), "\n")
