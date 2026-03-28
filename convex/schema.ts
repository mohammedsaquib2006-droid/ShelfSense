import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export default defineSchema({
  products: defineTable({
    item_name: v.string(),
    expiry_date: v.string(),
    quantity: v.number(),
    barcode: v.optional(v.string()),
  }),
});
