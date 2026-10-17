#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { PointyhatStack } from "../lib/pointyhat-stack";

const app = new cdk.App();
new PointyhatStack(app, "PointyhatStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION || "us-east-1",
  },
});
