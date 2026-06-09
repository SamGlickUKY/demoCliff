#!/usr/bin/env Rscript

# Plot state-level all-industry shift-share exposure against fertility changes.
# The historical script filename is retained, but outputs and figure labels now
# refer to the all-industry shift-share measure rather than construction.

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
changes_path <- if (length(args) >= 3) args[[3]] else default_path("processed_data", "state_shift_share_all_industries_tfr_pct_changes_2005_2010.parquet")
plot_path <- if (length(args) >= 4) args[[4]] else default_path("results", "state_shift_share_all_industries_tfr_pct_changes_2005_2010.png")

if (!file.exists(shift_share_path)) {
  stop("Missing all-industry shift-share input: ", shift_share_path,
       ". Run scripts/calculate_shift_share_construction_2005_2010.R first.")
}

start_year <- 2005L
end_year <- 2010L

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

changes <- merge(shift_share, tfr_wide, by = "state_fips", all = FALSE)
setcolorder(changes, c(
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
setorder(changes, state_fips)

if (nrow(changes) != 51L) {
  stop("Expected 51 states/DC in merged output; observed ", nrow(changes))
}
for (col in c("shift_share_all_industries_2005_2010", "tfr_pct_change")) {
  if (any(!is.finite(changes[[col]]))) {
    stop("Non-finite values detected in ", col)
  }
}

cat("Shift-share range:",
    round(min(changes$shift_share_all_industries_2005_2010), 4), "to",
    round(max(changes$shift_share_all_industries_2005_2010), 4), "\n")
cat("TFR pct change range:",
    round(min(changes$tfr_pct_change), 2), "to",
    round(max(changes$tfr_pct_change), 2), "\n")
cat("Correlation:",
    round(cor(changes$shift_share_all_industries_2005_2010, changes$tfr_pct_change), 4), "\n")

dir.create(dirname(changes_path), recursive = TRUE, showWarnings = FALSE)
arrow::write_parquet(as.data.frame(changes), changes_path)

plot <- ggplot(
  changes,
  aes(
    x = shift_share_all_industries_2005_2010,
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
  scale_x_continuous(labels = function(x) paste0(round(x, 1), "%")) +
  scale_y_continuous(labels = function(y) paste0(y, "%")) +
  labs(
    title = "All-industry shift-share exposure and fertility changes",
    subtitle = "All-industry shift-share uses 2005 state NAICS-2 employment shares weighted by national industry employment growth, 2005–2010",
    x = "All-industry shift-share exposure (weighted national employment growth), 2005–2010",
    y = "TFR percent change, 2005–2010",
    caption = "All-industry shift-share uses BLS QCEW NAICS-2 employment; fertility data use CDC WONDER births and population denominators."
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
