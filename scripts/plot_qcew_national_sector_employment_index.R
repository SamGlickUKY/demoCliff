#!/usr/bin/env Rscript

# Plot national 2-digit NAICS QCEW employment indexed to a base year.

suppressPackageStartupMessages({
  library(arrow)
  library(data.table)
  library(ggplot2)
})

args <- commandArgs(trailingOnly = TRUE)
input_path <- if (length(args) >= 1) args[[1]] else "processed_data/qcew_national_naics2_annual_employment_index_1990_2025.parquet"
output_path <- if (length(args) >= 2) args[[2]] else "results/qcew_national_naics2_employment_index_1990_2025.png"
base_year <- if (length(args) >= 3) as.integer(args[[3]]) else 2004
index_col <- paste0("employment_index_", base_year)

sector_order <- c(
  "11", "21", "22", "23", "31-33", "42", "44-45", "48-49",
  "51", "52", "53", "54", "55", "56", "61", "62", "71", "72",
  "81", "92", "99"
)

df <- as.data.table(dplyr::collect(arrow::open_dataset(input_path)))
if (!index_col %in% names(df)) {
  stop("Missing index column: ", index_col)
}

df$employment_index <- as.numeric(df[[index_col]])
df$naics2 <- factor(df$naics2, levels = sector_order)
df$facet_label <- paste0(as.character(df$naics2), " — ", df$sector_label)
df$facet_label <- factor(df$facet_label, levels = unique(df$facet_label[order(df$naics2)]))

dir.create(dirname(output_path), recursive = TRUE, showWarnings = FALSE)

plot <- ggplot(df, aes(x = year, y = employment_index)) +
  geom_hline(yintercept = 100, linetype = "dashed", linewidth = 0.35, color = "gray55") +
  geom_line(color = "#1f77b4", linewidth = 0.65) +
  geom_point(color = "#1f77b4", size = 0.45) +
  facet_wrap(~ facet_label, ncol = 4, scales = "free_y") +
  scale_x_continuous(breaks = seq(1990, 2025, by = 5)) +
  labs(
    title = paste0("National QCEW sector employment indexed to ", base_year),
    subtitle = paste0(
      "Annual employment is the average of quarterly average monthly employment; ",
      base_year, " = 100"
    ),
    x = NULL,
    y = paste0("Employment index (", base_year, " = 100)"),
    caption = "Source: BLS QCEW state NAICS-2 quarterly panel aggregated to national annual sector employment."
  ) +
  theme_minimal(base_size = 9) +
  theme(
    plot.title = element_text(face = "bold", size = 14),
    plot.subtitle = element_text(size = 10),
    strip.text = element_text(face = "bold", size = 7.5, hjust = 0),
    axis.text.x = element_text(angle = 45, hjust = 1, size = 6.5),
    panel.grid.minor = element_blank(),
    panel.spacing = unit(0.75, "lines")
  )

ggsave(output_path, plot, width = 14, height = 12, dpi = 300)
cat("Wrote", output_path, "\n")
