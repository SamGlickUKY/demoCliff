#!/usr/bin/env Rscript

# Plot state-level percent changes in construction employment and fertility
# rates from 2005 to 2010.

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

qcew_path <- if (length(args) >= 1) args[[1]] else default_path("processed_data", "qcew_state_naics2_quarterly_panel_1990_2025.parquet")
tfr_path <- if (length(args) >= 2) args[[2]] else default_path("processed_data", "tfr_state_year_1995_2024.parquet")
changes_path <- if (length(args) >= 3) args[[3]] else default_path("processed_data", "state_construction_tfr_pct_changes_2005_2010.parquet")
plot_path <- if (length(args) >= 4) args[[4]] else default_path("results", "state_construction_tfr_pct_changes_2005_2010.png")

start_year <- 2005L
end_year <- 2010L
construction_naics <- "23"

state_abbrev <- data.table(
  state = c(state.name, "District of Columbia"),
  state_label = c(state.abb, "DC")
)

construction_annual <- arrow::open_dataset(qcew_path) %>%
  filter(
    naics2 == construction_naics,
    year %in% c(start_year, end_year)
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
  old = c(as.character(start_year), as.character(end_year)),
  new = c("construction_employment_2005", "construction_employment_2010")
)
construction_wide[, construction_employment_pct_change :=
  100 * (construction_employment_2010 / construction_employment_2005 - 1)]

tfr_wide <- arrow::open_dataset(tfr_path) %>%
  filter(year %in% c(start_year, end_year)) %>%
  select(state_code, state, year, tfr) %>%
  collect() %>%
  as.data.table() %>%
  dcast(state_code + state ~ year, value.var = "tfr")
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

changes <- merge(construction_wide, tfr_wide, by = "state_fips", all = FALSE)
changes <- merge(changes, state_abbrev, by = "state", all.x = TRUE)
setcolorder(changes, c(
  "state_fips",
  "state",
  "state_label",
  "construction_employment_2005",
  "construction_employment_2010",
  "construction_employment_pct_change",
  "tfr_2005",
  "tfr_2010",
  "tfr_pct_change",
  "tfr_level_change",
  "fertility_rate_per_1000_change"
))
setorder(changes, state_fips)

if (nrow(changes) != 51L) {
  stop("Expected 51 states/DC in merged output; observed ", nrow(changes))
}

for (col in c("construction_employment_pct_change", "tfr_pct_change")) {
  if (any(!is.finite(changes[[col]]))) {
    stop("Non-finite values detected in ", col)
  }
}

dir.create(dirname(changes_path), recursive = TRUE, showWarnings = FALSE)
arrow::write_parquet(as.data.frame(changes), changes_path)

plot_df <- copy(changes)
plot <- ggplot(
  plot_df,
  aes(
    x = construction_employment_pct_change,
    y = tfr_pct_change
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
  scale_x_continuous(labels = function(x) paste0(x, "%")) +
  scale_y_continuous(labels = function(y) paste0(y, "%")) +
  labs(
    title = "State construction employment and fertility changes, 2005–2010",
    subtitle = "Construction employment is annual average QCEW NAICS 23 employment; TFR is births per woman ages 15–44",
    x = "Construction employment percent change, 2005–2010",
    y = "TFR percent change, 2005–2010",
    caption = "Sources: BLS QCEW state NAICS-2 quarterly panel; CDC WONDER births and population denominators."
  ) +
  theme_minimal(base_size = 11) +
  theme(
    plot.title = element_text(face = "bold"),
    panel.grid.minor = element_blank()
  )

dir.create(dirname(plot_path), recursive = TRUE, showWarnings = FALSE)
ggsave(plot_path, plot, width = 8, height = 6, dpi = 300)

cat("Wrote", changes_path, "\n")
cat("Wrote", plot_path, "\n")
cat("Construction employment pct change range:",
    round(min(changes$construction_employment_pct_change), 2), "to",
    round(max(changes$construction_employment_pct_change), 2), "\n")
cat("TFR pct change range:",
    round(min(changes$tfr_pct_change), 2), "to",
    round(max(changes$tfr_pct_change), 2), "\n")
