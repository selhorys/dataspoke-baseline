/**
 * Component-level wiring tests for ValidationConfForm.
 *
 * Parameter values are opaque strings. This exercises the actual multiline
 * control, React Hook Form state, Zod resolver, and PUT serialization together
 * so browser input behavior cannot silently normalize the stored value.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ValidationConfFormValues, ValidationConfPutRequest } from "@/types/validation";
import { ValidationConfForm } from "./validation-conf-form";

describe("ValidationConfForm — parameter value round-trip", () => {
  it("preserves spaces, tabs, and newlines byte-for-byte on edit submit", async () => {
    const opaqueValue = "  first\tcolumn\nsecond line  ";
    const defaultValues: ValidationConfFormValues = {
      description: "Existing validation config",
      variables: [{ name: "row_cnt", description: "Daily row count" }],
      attribute: { cadence_unit: 86400, cadence_offset: 0 },
      parameter: [
        { name: "threshold", value: opaqueValue, description: "Pipeline setting" },
      ],
    };
    const onSubmit = vi.fn<(body: ValidationConfPutRequest) => void>();

    render(
      <>
        <ValidationConfForm
          formId="validation-conf"
          defaultValues={defaultValues}
          onSubmit={onSubmit}
        />
        <button type="submit" form="validation-conf">Save</button>
      </>,
    );

    const valueControl = screen.getByRole("textbox", { name: "Parameter value 1" });
    expect(valueControl.tagName).toBe("TEXTAREA");
    expect((valueControl as HTMLTextAreaElement).value).toBe(opaqueValue);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0].parameter?.[0]?.value).toBe(opaqueValue);
  });
});
