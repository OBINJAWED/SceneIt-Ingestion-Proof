#!/usr/bin/env node
// Validate one real JSON HTTP emission against the checked-in OpenAPI response
// schema. js-yaml is resolved from locked Orval, which already parses this spec.
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const orvalRequire = createRequire(fs.realpathSync(
  path.join(root, "lib", "api-spec", "node_modules", "orval", "package.json"),
));
const yaml = orvalRequire("js-yaml");
const document = yaml.load(
  fs.readFileSync(path.join(root, "lib", "api-spec", "openapi.yaml"), "utf8"),
);

function resolveRef(value) {
  if (!value?.$ref) return value;
  if (!value.$ref.startsWith("#/")) throw new Error(`External ref is unsupported: ${value.$ref}`);
  return value.$ref.slice(2).split("/").reduce((node, part) => node[part], document);
}

function validate(schemaValue, value, location = "$") {
  const schema = resolveRef(schemaValue);
  if (!schema) throw new Error(`${location}: missing schema`);
  if (Array.isArray(schema.type)) {
    const failures = [];
    for (const type of schema.type) {
      try {
        validate({ ...schema, type }, value, location);
        return;
      } catch (error) {
        failures.push(error.message);
      }
    }
    throw new Error(`${location}: type union did not match (${failures.join("; ")})`);
  }
  if (schema.nullable && value === null) return;
  if (schema.allOf) {
    for (const item of schema.allOf) validate(item, value, location);
  }
  if (schema.anyOf || schema.oneOf) {
    const choices = schema.anyOf || schema.oneOf;
    const failures = [];
    const matches = choices.filter((item) => {
      try { validate(item, value, location); return true; }
      catch (error) { failures.push(error.message); return false; }
    });
    if (matches.length === 0 || (schema.oneOf && matches.length !== 1)) {
      throw new Error(`${location}: union did not match (${failures.join("; ")})`);
    }
    return;
  }
  if (schema.enum && !schema.enum.some((item) => Object.is(item, value))) {
    throw new Error(`${location}: value is outside enum`);
  }
  if (Object.hasOwn(schema, "const") && !Object.is(schema.const, value)) {
    throw new Error(`${location}: value does not match const`);
  }
  if (!schema.type) return;
  if (schema.type === "null") {
    if (value !== null) throw new Error(`${location}: expected null`);
  } else if (schema.type === "object") {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error(`${location}: expected object`);
    }
    for (const name of schema.required || []) {
      if (!Object.hasOwn(value, name)) throw new Error(`${location}.${name}: required`);
    }
    for (const [name, child] of Object.entries(schema.properties || {})) {
      if (Object.hasOwn(value, name)) validate(child, value[name], `${location}.${name}`);
    }
    if (schema.additionalProperties === false) {
      const known = new Set(Object.keys(schema.properties || {}));
      const extra = Object.keys(value).find((name) => !known.has(name));
      if (extra) throw new Error(`${location}.${extra}: additional property`);
    }
  } else if (schema.type === "array") {
    if (!Array.isArray(value)) throw new Error(`${location}: expected array`);
    if (schema.minItems != null && value.length < schema.minItems) throw new Error(`${location}: too short`);
    if (schema.maxItems != null && value.length > schema.maxItems) throw new Error(`${location}: too long`);
    value.forEach((item, index) => validate(schema.items, item, `${location}[${index}]`));
  } else if (schema.type === "string") {
    if (typeof value !== "string") throw new Error(`${location}: expected string`);
    if (schema.minLength != null && value.length < schema.minLength) throw new Error(`${location}: too short`);
    if (schema.maxLength != null && value.length > schema.maxLength) throw new Error(`${location}: too long`);
    if (schema.format === "uuid" && !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
      throw new Error(`${location}: invalid uuid`);
    }
    if (schema.format === "date-time" && Number.isNaN(Date.parse(value))) {
      throw new Error(`${location}: invalid date-time`);
    }
  } else if (schema.type === "integer") {
    if (!Number.isInteger(value)) throw new Error(`${location}: expected integer`);
  } else if (schema.type === "number") {
    if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${location}: expected number`);
  } else if (schema.type === "boolean" && typeof value !== "boolean") {
    throw new Error(`${location}: expected boolean`);
  }
  if (typeof value === "number") {
    if (schema.minimum != null && value < schema.minimum) throw new Error(`${location}: below minimum`);
    if (schema.maximum != null && value > schema.maximum) throw new Error(`${location}: above maximum`);
  }
}

const [method, apiPath, status] = process.argv.slice(2);
if (!method || !apiPath || !status) {
  throw new Error("usage: validate-openapi-response.mjs METHOD /path STATUS < response.json");
}
const operation = document.paths?.[apiPath]?.[method.toLowerCase()];
if (!operation) throw new Error(`OpenAPI operation is missing: ${method} ${apiPath}`);
let response = operation.responses?.[status] || operation.responses?.default;
response = resolveRef(response);
if (!response) throw new Error(`OpenAPI response is missing: ${method} ${apiPath} ${status}`);
const schema = response.content?.["application/json"]?.schema;
if (!schema) throw new Error(`JSON response schema is missing: ${method} ${apiPath} ${status}`);
validate(schema, JSON.parse(fs.readFileSync(0, "utf8")));