#!/usr/bin/env Rscript

# Aggregate IPEDS first-time college enrollments to state-year level and
# relate them to state-year births 18 years earlier.
#
# Run from the repository root:
#   Rscript scripts/aggregate_ipeds_first_time_enrollment.R
#   Rscript scripts/aggregate_ipeds_first_time_enrollment.R --refresh       # re-fetch IPEDS
#   Rscript scripts/aggregate_ipeds_first_time_enrollment.R --use-existing  # skip API, use raw_data/ipeds.parquet fallback

suppressPackageStartupMessages({
  library(arrow)
  library(data.table)
  library(dplyr)
  library(educationdata)
  library(fixest)
})

args <- commandArgs(trailingOnly = TRUE)
refresh <- "--refresh" %in% args
use_existing <- "--use-existing" %in% args

# Allow slow API pages to complete when the Urban Institute API is under load.
httr::set_config(httr::timeout(600))
options(timeout = max(600, getOption("timeout")))

raw_dir <- "raw_data"
processed_dir <- "processed_data"
results_dir <- "results"

ipeds_years <- 2000:2022
births_path <- file.path(processed_dir, "births_state_year_1995_2024.parquet")
existing_ipeds_path <- file.path(raw_dir, "ipeds.parquet")
ipeds_college_dir <- file.path(processed_dir, "ipeds_first_time_college_by_year")
ipeds_college_path <- file.path(processed_dir, "ipeds_first_time_college_2000_2022.parquet")
ipeds_state_year_path <- file.path(processed_dir, "ipeds_first_time_state_year_2000_2022.parquet")
analysis_path <- file.path(processed_dir, "state_year_births_lag18_first_time_enrollment.parquet")
reg_txt_path <- file.path(results_dir, "births_lag18_first_time_enrollment_regressions.txt")
reg_parquet_path <- file.path(results_dir, "births_lag18_first_time_enrollment_regressions.parquet")

dir.create(raw_dir, showWarnings = FALSE, recursive = TRUE)
dir.create(processed_dir, showWarnings = FALSE, recursive = TRUE)
dir.create(results_dir, showWarnings = FALSE, recursive = TRUE)
dir.create(ipeds_college_dir, showWarnings = FALSE, recursive = TRUE)

read_parquet_dt <- function(path, select_cols = NULL) {
  if (!file.exists(path)) stop("Missing parquet input: ", path)
  dataset <- arrow::open_dataset(path)
  if (!is.null(select_cols)) dataset <- dplyr::select(dataset, dplyr::all_of(select_cols))
  as.data.table(dplyr::collect(dataset))
}

write_parquet_dt <- function(dt, path) {
  dir.create(dirname(path), showWarnings = FALSE, recursive = TRUE)
  arrow::write_parquet(as.data.frame(dt), path)
}

fetch_ipeds_first_time <- function(year) {
  # level_of_study must be the label "undergraduate". class_level = 1 selects
  # first-time entrants; class_level = 99 would fetch all class levels.
  educationdata::get_education_data(
    level = "college-university",
    source = "ipeds",
    topic = "fall-enrollment",
    filters = list(
      year = year,
      level_of_study = "undergraduate",
      sex = 99,
      race = 99,
      ftpt = 99,
      degree_seeking = 1,
      class_level = 1
    ),
    subtopic = list("race", "sex")
  )
}

fetch_ipeds_first_time_with_retry <- function(year, attempts = 5, sleep_seconds = 30) {
  last_error <- NULL
  for (attempt in seq_len(attempts)) {
    out <- tryCatch(fetch_ipeds_first_time(year), error = function(e) e)
    if (!inherits(out, "error")) return(as.data.table(out))

    last_error <- out
    message(
      "IPEDS year ", year, " failed on attempt ", attempt, "/", attempts,
      ": ", conditionMessage(out)
    )
    if (attempt < attempts) Sys.sleep(sleep_seconds * attempt)
  }
  stop(last_error)
}

load_existing_ipeds_fallback <- function() {
  warning(
    "Falling back to ", existing_ipeds_path,
    " because the educationdata API is unavailable. Full 2000-2022 coverage ",
    "requires a complete local parquet fallback or rerunning when the API is available.",
    call. = FALSE
  )

  dt <- read_parquet_dt(existing_ipeds_path)
  if ("year1_enrolls" %in% names(dt)) {
    dt <- dt[year %in% ipeds_years & !is.na(year1_enrolls)]
    return(dt[, .(
      unitid,
      year = as.integer(year),
      fips = as.integer(fips),
      enrollment_fall = as.numeric(year1_enrolls)
    )])
  }

  required <- c("unitid", "year", "fips", "enrollment_fall")
  missing <- setdiff(required, names(dt))
  if (length(missing)) stop("Fallback IPEDS parquet is missing columns: ", paste(missing, collapse = ", "))
  dt[year %in% ipeds_years, .(
    unitid,
    year = as.integer(year),
    fips = as.integer(fips),
    enrollment_fall = as.numeric(enrollment_fall)
  )]
}

ipeds_source <- "educationdata"

if (refresh && file.exists(ipeds_college_path)) unlink(ipeds_college_path)
if (refresh) unlink(file.path(ipeds_college_dir, sprintf("ipeds_first_time_%s.parquet", ipeds_years)))

if (use_existing) {
  ipeds_source <- existing_ipeds_path
  ipeds_college <- load_existing_ipeds_fallback()
} else if (refresh || !file.exists(ipeds_college_path)) {
  message("Fetching IPEDS first-time fall enrollment data from educationdata...")
  message("Caching one parquet file per year in ", ipeds_college_dir, " so interrupted runs can resume.")

  ipeds_college <- tryCatch(
    rbindlist(lapply(ipeds_years, function(year) {
      year_path <- file.path(ipeds_college_dir, sprintf("ipeds_first_time_%s.parquet", year))
      if (!refresh && file.exists(year_path)) {
        message("Using cached IPEDS year ", year, ": ", year_path)
        return(read_parquet_dt(year_path))
      }

      message("Fetching IPEDS year ", year, "...")
      year_data <- fetch_ipeds_first_time_with_retry(year)
      write_parquet_dt(year_data, year_path)
      year_data
    }), fill = TRUE),
    error = function(e) {
      warning("educationdata fetch failed: ", conditionMessage(e), call. = FALSE)
      NULL
    }
  )

  if (is.null(ipeds_college) || nrow(ipeds_college) == 0) {
    ipeds_source <- existing_ipeds_path
    ipeds_college <- load_existing_ipeds_fallback()
  } else {
    write_parquet_dt(ipeds_college, ipeds_college_path)
  }
} else {
  message("Using cached college-level IPEDS data: ", ipeds_college_path)
  ipeds_college <- read_parquet_dt(ipeds_college_path)
}

ipeds_college <- as.data.table(ipeds_college)
ipeds_college[, `:=`(
  state_fips = sprintf("%02d", as.integer(fips)),
  year = as.integer(year),
  enrollment_fall = as.numeric(enrollment_fall)
)]

state_year_enrollment <- ipeds_college[, .(
  first_time_enrollments = sum(enrollment_fall, na.rm = TRUE),
  n_colleges = uniqueN(unitid)
), by = .(state_fips, year)]
setorder(state_year_enrollment, state_fips, year)
write_parquet_dt(state_year_enrollment, ipeds_state_year_path)

births <- read_parquet_dt(
  births_path,
  c("state", "state_code", "year", "births", "source_db")
)
births[, `:=`(
  state_fips = sprintf("%02d", as.integer(state_code)),
  year = as.integer(year),
  births = as.numeric(births)
)]

state_lookup <- unique(births[, .(state_fips, state)])
births_lag18 <- births[, .(
  state_fips,
  birth_year = year,
  births_lag18 = births,
  births_source_db = source_db
)]

analysis_df <- copy(state_year_enrollment)
analysis_df[, birth_year := year - 18L]
analysis_df <- merge(analysis_df, state_lookup, by = "state_fips", all.x = TRUE)
analysis_df <- merge(analysis_df, births_lag18, by = c("state_fips", "birth_year"), all.x = TRUE)
setcolorder(analysis_df, c("state_fips", "state", setdiff(names(analysis_df), c("state_fips", "state"))))
setorder(analysis_df, state_fips, year)
write_parquet_dt(analysis_df, analysis_path)

reg_data <- analysis_df[
  !is.na(births_lag18) & !is.na(first_time_enrollments) &
    births_lag18 > 0 & first_time_enrollments > 0
]

if (nrow(reg_data) == 0) {
  stop("No matched state-year observations with births from t-18. Check input files.")
}

models_enroll_on_births <- list(
  "enroll_on_births_pooled" = feols(
    first_time_enrollments ~ births_lag18,
    data = reg_data,
    vcov = ~ state_fips
  ),
  "log_enroll_on_log_births_pooled" = feols(
    log(first_time_enrollments) ~ log(births_lag18),
    data = reg_data,
    vcov = ~ state_fips
  ),
  "enroll_on_births_state_year_fe" = feols(
    first_time_enrollments ~ births_lag18 | state_fips + year,
    data = reg_data,
    vcov = ~ state_fips
  ),
  "log_enroll_on_log_births_state_year_fe" = feols(
    log(first_time_enrollments) ~ log(births_lag18) | state_fips + year,
    data = reg_data,
    vcov = ~ state_fips
  )
)

models_births_on_enroll <- list(
  "births_on_enroll_pooled" = feols(
    births_lag18 ~ first_time_enrollments,
    data = reg_data,
    vcov = ~ state_fips
  ),
  "log_births_on_log_enroll_pooled" = feols(
    log(births_lag18) ~ log(first_time_enrollments),
    data = reg_data,
    vcov = ~ state_fips
  ),
  "births_on_enroll_state_year_fe" = feols(
    births_lag18 ~ first_time_enrollments | state_fips + year,
    data = reg_data,
    vcov = ~ state_fips
  ),
  "log_births_on_log_enroll_state_year_fe" = feols(
    log(births_lag18) ~ log(first_time_enrollments) | state_fips + year,
    data = reg_data,
    vcov = ~ state_fips
  )
)

coef_table <- function(models, direction) {
  rbindlist(lapply(names(models), function(model_name) {
    ct <- as.data.table(coeftable(models[[model_name]]), keep.rownames = "term")
    setnames(ct, old = names(ct)[2:5], new = c("estimate", "std_error", "statistic", "p_value"))
    ct[, `:=`(
      direction = direction,
      model = model_name,
      nobs = nobs(models[[model_name]])
    )]
    ct[, .(direction, model, nobs, term, estimate, std_error, statistic, p_value)]
  }), fill = TRUE)
}

regression_coefs <- rbindlist(list(
  coef_table(models_births_on_enroll, "births_lag18_on_enrollment_t"),
  coef_table(models_enroll_on_births, "enrollment_t_on_births_lag18")
), fill = TRUE)
write_parquet_dt(regression_coefs, reg_parquet_path)

sink(reg_txt_path)
cat("IPEDS first-time fall enrollment aggregation and birth-lag regressions\n")
cat("=================================================================\n\n")
cat("IPEDS source: ", ipeds_source, "\n", sep = "")
cat("College-level IPEDS cache: ", ipeds_college_path, "\n", sep = "")
cat("State-year enrollment file: ", ipeds_state_year_path, "\n", sep = "")
cat("Analysis file: ", analysis_path, "\n", sep = "")
cat("Coefficient parquet: ", reg_parquet_path, "\n\n", sep = "")
cat("IPEDS years: ", min(ipeds_years), "-", max(ipeds_years), "\n", sep = "")
cat("Matched regression years: ", min(reg_data$year), "-", max(reg_data$year), "\n", sep = "")
cat("Matched observations: ", nrow(reg_data), "\n", sep = "")
cat("States matched: ", uniqueN(reg_data$state_fips), "\n\n", sep = "")

cat("Summary of matched analysis data\n")
print(summary(reg_data[, .(year, birth_year, births_lag18, first_time_enrollments, n_colleges)]))

cat("\n\nRequested direction: births_{t-18} on first-time enrollments_t\n")
print(etable(models_births_on_enroll, dict = c(first_time_enrollments = "First-time enrollments", births_lag18 = "Births t-18")))

cat("\n\nAlternative timing direction: first-time enrollments_t on births_{t-18}\n")
print(etable(models_enroll_on_births, dict = c(first_time_enrollments = "First-time enrollments", births_lag18 = "Births t-18")))
sink()

message("Wrote: ", ipeds_state_year_path)
message("Wrote: ", analysis_path)
message("Wrote: ", reg_txt_path)
message("Wrote: ", reg_parquet_path)
