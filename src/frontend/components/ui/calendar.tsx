"use client";

import * as React from "react";
import { DayPicker } from "react-day-picker";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type CalendarProps = React.ComponentProps<typeof DayPicker>;

/**
 * shadcn-style wrapper around react-day-picker's DayPicker.
 * Themed with the project's Tailwind tokens; supports range selection.
 */
export function Calendar({
  className,
  classNames,
  showOutsideDays = true,
  ...props
}: CalendarProps) {
  return (
    <DayPicker
      showOutsideDays={showOutsideDays}
      // navLayout="around" renders the prev/next buttons as inline siblings of
      // the month caption *inside* each month (rather than the default single
      // <Nav> that sits at the DayPicker's outer edge). That makes every
      // calendar own its arrows, reading [‹] [Month ▾] [Year ▾] [›].
      navLayout="around"
      className={cn("p-1", className)}
      classNames={{
        months: "flex flex-col sm:flex-row gap-2",
        // `relative` anchors the absolutely-positioned prev/next arrows to this
        // calendar's own caption row.
        month: "flex flex-col gap-3 relative",
        month_caption: "flex justify-center pt-1 relative items-center h-9",
        caption_label: "text-sm font-medium",
        // captionLayout="dropdown": render the native month/year selects as the
        // visible themed controls and hide rdp's overlay label (it renders a
        // duplicate caption_label span on top of each select).
        dropdowns: "flex items-center justify-center gap-1.5",
        // The native <select> is the visible control; rdp also draws a
        // caption_label overlay span inside each dropdown_root — hide it.
        dropdown_root: "relative inline-flex items-center [&>[aria-hidden=true]]:hidden",
        dropdown:
          "rounded-md border bg-background px-1.5 py-1 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        months_dropdown: "appearance-none",
        years_dropdown: "appearance-none",
        // With navLayout="around" no single <Nav> wrapper is rendered; the
        // prev/next buttons sit beside the caption. Pin each to its calendar's
        // own caption row so the result reads [‹] [Month ▾] [Year ▾] [›].
        nav: "flex items-center gap-1",
        button_previous: cn(
          buttonVariants({ variant: "outline" }),
          "absolute left-1 top-1 z-10 h-7 w-7 bg-transparent p-0 opacity-70 hover:opacity-100",
        ),
        button_next: cn(
          buttonVariants({ variant: "outline" }),
          "absolute right-1 top-1 z-10 h-7 w-7 bg-transparent p-0 opacity-70 hover:opacity-100",
        ),
        month_grid: "w-full border-collapse space-y-1",
        weekdays: "flex",
        weekday: "text-muted-foreground rounded-md w-8 font-normal text-[0.8rem]",
        week: "flex w-full mt-1",
        day: cn(
          "relative p-0 text-center text-sm focus-within:relative focus-within:z-20",
          "[&:has([data-range-middle])]:bg-primary/15",
          "[&:has([data-range-start])]:rounded-l-md [&:has([data-range-end])]:rounded-r-md",
          "[&:has([data-range-middle])]:first:rounded-l-md [&:has([data-range-middle])]:last:rounded-r-md",
        ),
        day_button: cn(
          buttonVariants({ variant: "ghost" }),
          "h-8 w-8 p-0 font-normal aria-selected:opacity-100",
        ),
        range_start:
          "rounded-l-md [&>button]:bg-primary [&>button]:text-primary-foreground [&>button]:hover:bg-primary [&>button]:hover:text-primary-foreground",
        range_end:
          "rounded-r-md [&>button]:bg-primary [&>button]:text-primary-foreground [&>button]:hover:bg-primary [&>button]:hover:text-primary-foreground",
        range_middle:
          "[&>button]:!bg-primary/15 [&>button]:!text-foreground [&>button]:hover:!bg-primary/15",
        selected:
          "[&>button]:bg-primary [&>button]:text-primary-foreground [&>button]:hover:bg-primary [&>button]:hover:text-primary-foreground",
        today: "[&>button]:bg-accent [&>button]:text-accent-foreground",
        outside: "text-muted-foreground opacity-50",
        disabled: "text-muted-foreground opacity-50",
        hidden: "invisible",
        ...classNames,
      }}
      components={{
        Chevron: ({ orientation, className: chevronClassName, ...chevronProps }) =>
          orientation === "left" ? (
            <ChevronLeft className={cn("h-4 w-4", chevronClassName)} {...chevronProps} />
          ) : (
            <ChevronRight className={cn("h-4 w-4", chevronClassName)} {...chevronProps} />
          ),
      }}
      {...props}
    />
  );
}
Calendar.displayName = "Calendar";
