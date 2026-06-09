#!/usr/bin/env Rscript

# Calculate an all-industry shift-share exposure by state.
#
# For each state s, the main shift-share variable is:
#   sum_k (state s employment share in industry k in 2005
#          * national 2005-2010 percent change in industry k employment)
# where k indexes NAICS-2 industries in the QCEW state panel. Output filenames
# use "all_industries" to match the actual all-industry shift-share measure.

suppressPackageStartupMessages({
  library(arrow)
  library(data.table)
  library(dplyr)
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
output_path <- if (length(args) >= 2) args[[2]] else default_path("processed_data", "state_shift_share_all_industries_2005_2010.parquet")

base_year <- 2005L
end_year <- 2010L
state_fips_51 <- c(
  "01", "02", "04", "05", "06", "08", "09", "10", "11", "12",
  "13", "15", "16", "17", "18", "19", "20", "21", "22", "23",
  "24", "25", "26", "27", "28", "29", "30", "31", "32", "33",
  "34", "35", "36", "37", "38", "39", "40", "41", "42", "44",
  "45", "46", "47", "48", "49", "50", "51", "53", "54", "55",
  "56"
)
state_lookup <- data.table(
  state_fips = state_fips_51,
  state = c(
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "District of Columbia", "Florida", "Georgia",
    "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
    "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
    "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "West Virginia", "Wisconsin", "Wyoming"
  ),
  state_label = c(state.abb[match(c(
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware"), state.name)], "DC", state.abb[match(c(
    "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts",
    "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska",
    "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
    "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
    "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming"
  ), state.name)])
)

qcew <- arrow::open_dataset(qcew_path) %>%
  filter(
    state_fips %in% state_fips_51,
    year %in% c(base_year, end_year)
  ) %>%
  select(state_fips, year, quarter, naics2, employment, state_total_employment) %>%
  collect() %>%
  as.data.table()

industry_annual <- qcew[, .(
  industry_employment = mean(employment, na.rm = TRUE)
), by = .(state_fips, year, naics2)]
industry_annual[is.nan(industry_employment), industry_employment := NA_real_]

state_total_annual <- unique(qcew[, .(
  state_fips,
  year,
  quarter,
  state_total_employment
)])[, .(
  total_employment = mean(state_total_employment, na.rm = TRUE)
), by = .(state_fips, year)]
state_total_annual[is.nan(total_employment), total_employment := NA_real_]

base_industry <- merge(
  industry_annual[year == base_year, .(
    state_fips,
    naics2,
    industry_employment_2005 = industry_employment
  )],
  state_total_annual[year == base_year, .(
    state_fips,
    total_employment_2005 = total_employment
  )],
  by = "state_fips",
  all = FALSE
)
base_industry[, state_industry_employment_share_2005 :=
  industry_employment_2005 / total_employment_2005]

national_industry <- industry_annual[, .(
  national_industry_employment = sum(industry_employment, na.rm = TRUE),
  contributing_states = sum(!is.na(industry_employment))
), by = .(naics2, year)]
national_wide <- dcast(
  national_industry,
  naics2 ~ year,
  value.var = c("national_industry_employment", "contributing_states")
)
setnames(
  national_wide,
  old = c(
    paste0("national_industry_employment_", base_year),
    paste0("national_industry_employment_", end_year),
    paste0("contributing_states_", base_year),
    paste0("contributing_states_", end_year)
  ),
  new = c(
    "national_industry_employment_2005",
    "national_industry_employment_2010",
    "contributing_states_2005",
    "contributing_states_2010"
  )
)
national_wide[, national_industry_employment_change_2005_2010 :=
  national_industry_employment_2010 - national_industry_employment_2005]
national_wide[, national_industry_employment_pct_change_2005_2010 :=
  100 * (national_industry_employment_2010 / national_industry_employment_2005 - 1)]

components <- merge(
  base_industry,
  national_wide,
  by = "naics2",
  all.x = TRUE
)
components[, shift_share_employment_change_component_2005_2010 :=
  state_industry_employment_share_2005 * national_industry_employment_change_2005_2010]
components[, shift_share_component_2005_2010 :=
  state_industry_employment_share_2005 * national_industry_employment_pct_change_2005_2010]

shift_share <- components[, .(
  total_employment_2005 = first(total_employment_2005),
  covered_industry_employment_2005 = sum(industry_employment_2005, na.rm = TRUE),
  industry_employment_share_sum_2005 = sum(state_industry_employment_share_2005, na.rm = TRUE),
  naics2_industry_count_2005 = sum(!is.na(state_industry_employment_share_2005)),
  naics2_industry_count_with_national_change = sum(
    !is.na(state_industry_employment_share_2005) &
      !is.na(national_industry_employment_pct_change_2005_2010)
  ),
  shift_share_all_industries_2005_2010 = sum(shift_share_component_2005_2010, na.rm = TRUE),
  shift_share_all_industries_employment_change_2005_2010 = sum(
    shift_share_employment_change_component_2005_2010,
    na.rm = TRUE
  )
), by = state_fips]

national_totals <- national_wide[, .(
  national_naics2_industry_count = .N,
  national_industry_employment_2005 = sum(national_industry_employment_2005, na.rm = TRUE),
  national_industry_employment_2010 = sum(national_industry_employment_2010, na.rm = TRUE),
  national_industry_employment_change_2005_2010 = sum(
    national_industry_employment_change_2005_2010,
    na.rm = TRUE
  )
)]
national_totals[, national_industry_employment_pct_change_2005_2010 :=
  100 * (national_industry_employment_2010 / national_industry_employment_2005 - 1)]
shift_share[, `:=`(
  national_naics2_industry_count = national_totals$national_naics2_industry_count,
  national_industry_employment_2005 = national_totals$national_industry_employment_2005,
  national_industry_employment_2010 = national_totals$national_industry_employment_2010,
  national_industry_employment_change_2005_2010 =
    national_totals$national_industry_employment_change_2005_2010,
  national_industry_employment_pct_change_2005_2010 =
    national_totals$national_industry_employment_pct_change_2005_2010
)]

shift_share <- merge(shift_share, state_lookup, by = "state_fips", all.x = TRUE)
setcolorder(shift_share, c(
  "state_fips",
  "state",
  "state_label",
  "total_employment_2005",
  "covered_industry_employment_2005",
  "industry_employment_share_sum_2005",
  "naics2_industry_count_2005",
  "naics2_industry_count_with_national_change",
  "national_naics2_industry_count",
  "national_industry_employment_2005",
  "national_industry_employment_2010",
  "national_industry_employment_change_2005_2010",
  "national_industry_employment_pct_change_2005_2010",
  "shift_share_all_industries_2005_2010",
  "shift_share_all_industries_employment_change_2005_2010"
))
setorder(shift_share, state_fips)

if (nrow(shift_share) != 51L) {
  stop("Expected 51 states/DC in output; observed ", nrow(shift_share))
}
for (col in c(
  "total_employment_2005",
  "industry_employment_share_sum_2005",
  "shift_share_all_industries_2005_2010",
  "shift_share_all_industries_employment_change_2005_2010"
)) {
  if (any(!is.finite(shift_share[[col]]))) {
    stop("Non-finite values detected in ", col)
  }
}

if (uniqueN(round(shift_share$shift_share_all_industries_2005_2010, 12)) == 1L) {
  stop("Shift-share variable has no state variation; check the formula")
}

if (any(shift_share$industry_employment_share_sum_2005 <= 0)) {
  stop("Some states have no covered 2005 industry employment shares")
}

# Save industry-level shocks/components next to the state-level output for auditability.
component_output_path <- sub("\\.parquet$", "_components.parquet", output_path)
component_output <- components[, .(
  state_fips,
  naics2,
  industry_employment_2005,
  total_employment_2005,
  state_industry_employment_share_2005,
  national_industry_employment_2005,
  national_industry_employment_2010,
  national_industry_employment_change_2005_2010,
  national_industry_employment_pct_change_2005_2010,
  shift_share_component_2005_2010,
  shift_share_employment_change_component_2005_2010,
  contributing_states_2005,
  contributing_states_2010
)]
setorder(component_output, state_fips, naics2)

output_dir <- dirname(output_path)
dir.create(output_dir, recursive = TRUE, showWarnings = FALSE)
arrow::write_parquet(as.data.frame(shift_share), output_path)
arrow::write_parquet(as.data.frame(component_output), component_output_path)

cat("Wrote", output_path, "\n")
cat("Wrote", component_output_path, "\n")
cat("National NAICS-2 industry count:", national_totals$national_naics2_industry_count, "\n")
cat("National all-industry employment percent change, 2005-2010:",
    round(national_totals$national_industry_employment_pct_change_2005_2010, 2), "\n")
cat("2005 state covered industry share range:",
    round(min(shift_share$industry_employment_share_sum_2005), 4), "to",
    round(max(shift_share$industry_employment_share_sum_2005), 4), "\n")
cat("All-industry percent-change shift-share range:",
    round(min(shift_share$shift_share_all_industries_2005_2010), 4), "to",
    round(max(shift_share$shift_share_all_industries_2005_2010), 4), "\n")
