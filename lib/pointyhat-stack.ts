// pointyhat-stack.ts
// PointyHat — CDK infrastructure stack
//
// Architecture:
//   CloudFront → S3 (index.html)
//   CloudFront → API Gateway
//     POST /search        → createJob    → starts Step Functions execution
//     POST /resume        → resumeJob    → SendTaskSuccess → SF resumes
//     GET  /job/{jobId}   → checkJob     → reads DynamoDB
//
//   Step Functions Express Workflow:
//     writerLambda (PENDING)
//     → JobRouter (Choice)
//         image → visionLambda → writerLambda (CLARIFICATION, waitForTaskToken)
//         text  → ambiguityLambda
//                   → needs clarification → writerLambda (CLARIFICATION, waitForTaskToken)
//                   → clear              → hackSearchLambda
//     → [user answers via POST /resume → resumeJob → SendTaskSuccess]
//     → hackSearchLambda
//     → rankerLambda
//     → writerLambda (COMPLETE)
//     any error → writerLambda (FAILED)
//
// SSM parameters required before first deploy:
//   /pointyhat/brave-api-key   (SecureString)
//   /pointyhat/logfire-token   (SecureString)

import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { Construct } from 'constructs';
import * as fs from 'fs';
import * as path from 'path';

// Shared bundling command for all Python Lambdas.
// Runs inside ARM64 Docker container during cdk deploy.
// Docker Desktop must be running before deploying.
const BUNDLING_CMD = [
  'bash', '-c',
  'pip install -r requirements.txt -t /asset-output && cp -au . /asset-output',
];

// Shared Lambda defaults — all Lambdas use Python 3.12 ARM64
const LAMBDA_DEFAULTS = {
  runtime:      lambda.Runtime.PYTHON_3_12,
  architecture: lambda.Architecture.ARM_64,
};

// Helper: create a Lambda with standard bundling
function makeLambda(
  scope: Construct,
  id: string,
  opts: {
    functionName: string;
    handler:      string;
    srcDir:       string;
    timeout:      cdk.Duration;
    memorySize:   number;
    environment?: Record<string, string>;
  }
): lambda.Function {
  return new lambda.Function(scope, id, {
    ...LAMBDA_DEFAULTS,
    functionName: opts.functionName,
    handler:      opts.handler,
    code: lambda.Code.fromAsset(
      path.join(__dirname, opts.srcDir),
      {
        bundling: {
          image:    lambda.Runtime.PYTHON_3_12.bundlingImage,
          platform: 'linux/arm64',
          command:  BUNDLING_CMD,
        },
      }
    ),
    timeout:     opts.timeout,
    memorySize:  opts.memorySize,
    environment: opts.environment ?? {},
  });
}

// Shared Bedrock policy — Sonnet via cross-region inference profile
const BEDROCK_POLICY = new iam.PolicyStatement({
  actions: [
    'bedrock:InvokeModel',
    'bedrock:GetInferenceProfile',
    'bedrock:ListInferenceProfiles',
  ],
  resources: ['*'],
});

export class PointyhatStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ============================================
    // DynamoDB — single job state table
    // Written only by writerLambda.
    // Read only by checkJob.
    // TTL clears jobs + images after 24 hours.
    // ============================================
    const jobsTable = new dynamodb.Table(this, 'JobsTable', {
      tableName:        'pointyhat-jobs',
      partitionKey:     { name: 'jobId', type: dynamodb.AttributeType.STRING },
      billingMode:      dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy:    cdk.RemovalPolicy.DESTROY,
    });

    // ============================================
    // SSM — secrets (pre-create before deploying)
    //   aws ssm put-parameter --name /pointyhat/brave-api-key \
    //     --value <key> --type SecureString --profile <your-profile>
    //   aws ssm put-parameter --name /pointyhat/logfire-token \
    //     --value <token> --type SecureString --profile <your-profile>
    // ============================================
    const braveApiKeyParam = ssm.StringParameter.fromSecureStringParameterAttributes(
      this, 'BraveApiKey', { parameterName: '/pointyhat/brave-api-key' }
    );

    const logfireTokenParam = ssm.StringParameter.fromSecureStringParameterAttributes(
      this, 'LogfireToken', { parameterName: '/pointyhat/logfire-token' }
    );

    // ============================================
    // hackSearchLambda
    // Brave Search + Shopify + URL filter + image fetch.
    // Invoked as a Step Functions task — not via API Gateway.
    // Unchanged from previous architecture.
    // ============================================
    const hackSearchLambda = makeLambda(this, 'HackSearch', {
      functionName: 'pointyhat-hacksearch',
      handler:      'hacksearch.lambda_handler',
      srcDir:       '../lambda/hacksearch',
      timeout:      cdk.Duration.seconds(60),
      memorySize:   512,
      environment:  { BRAVE_API_KEY_PARAM: '/pointyhat/brave-api-key' },
    });
    braveApiKeyParam.grantRead(hackSearchLambda);

    // ============================================
    // visionLambda
    // Extracted from doJob — runs vision_agent only.
    // Input:  { jobId, jobType, imageBase64, imageMimeType }
    // Output: OutfitAnalysis { colors, style_signals, outfit_context,
    //           hat_suggestions, suggested_hat_color, question, reasoning }
    // Memory: 1536MB — Pydantic AI + Bedrock vision + Logfire.
    // ============================================
    const visionLambda = makeLambda(this, 'VisionLambda', {
      functionName: 'pointyhat-vision',
      handler:      'visionLambda.lambda_handler',
      srcDir:       '../lambda/visionLambda',
      timeout:      cdk.Duration.seconds(30),
      memorySize:   1536,
      environment: {
        SONNET_MODEL_ID:     'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
        LOGFIRE_TOKEN_PARAM: '/pointyhat/logfire-token',
        TABLE_NAME:          jobsTable.tableName,
      },
    });
    visionLambda.addToRolePolicy(BEDROCK_POLICY);
    logfireTokenParam.grantRead(visionLambda);
    jobsTable.grantReadData(visionLambda); // reads imageBase64 for large images

    // ============================================
    // ambiguityLambda
    // Extracted from doJob — runs check_ambiguity only.
    // Input:  { jobId, jobType, query, refinedQuery }
    // Output: AmbiguityCheck { is_ambiguous, clarification? }
    // ============================================
    const ambiguityLambda = makeLambda(this, 'AmbiguityLambda', {
      functionName: 'pointyhat-ambiguity',
      handler:      'ambiguityLambda.lambda_handler',
      srcDir:       '../lambda/ambiguityLambda',
      timeout:      cdk.Duration.seconds(30),
      memorySize:   512,
      environment: {
        SONNET_MODEL_ID:     'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
        LOGFIRE_TOKEN_PARAM: '/pointyhat/logfire-token',
      },
    });
    ambiguityLambda.addToRolePolicy(BEDROCK_POLICY);
    logfireTokenParam.grantRead(ambiguityLambda);

    // ============================================
    // rankerLambda
    // Extracted from doJob — runs get_recommendations only.
    // Input:  { jobId, refinedQuery, hacksearchData }
    // Output: RecommendationSet { recommendations[3], reasoning }
    // ============================================
    const rankerLambda = makeLambda(this, 'RankerLambda', {
      functionName: 'pointyhat-ranker',
      handler:      'rankerLambda.lambda_handler',
      srcDir:       '../lambda/rankerLambda',
      timeout:      cdk.Duration.seconds(30),
      memorySize:   512,
      environment: {
        SONNET_MODEL_ID:     'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
        LOGFIRE_TOKEN_PARAM: '/pointyhat/logfire-token',
      },
    });
    rankerLambda.addToRolePolicy(BEDROCK_POLICY);
    logfireTokenParam.grantRead(rankerLambda);

    // ============================================
    // writerLambda
    // Single source of truth for all DynamoDB writes.
    // Called by Step Functions at every state transition.
    // writeType: PENDING | IN_PROGRESS | CLARIFICATION | COMPLETE | FAILED
    // On CLARIFICATION: stores taskToken for resumeJob to use later.
    // ============================================
    const writerLambda = makeLambda(this, 'WriterLambda', {
      functionName: 'pointyhat-writer',
      handler:      'writerLambda.lambda_handler',
      srcDir:       '../lambda/writerLambda',
      timeout:      cdk.Duration.seconds(10),
      memorySize:   128,
      environment:  { TABLE_NAME: jobsTable.tableName },
    });
    jobsTable.grantWriteData(writerLambda);

    // ============================================
    // createJob Lambda
    // Handles POST /search.
    // Validates input, generates jobId, starts SF execution.
    // No DynamoDB write — writerLambda owns all writes.
    // ============================================
    const createJob = makeLambda(this, 'CreateJob', {
      functionName: 'pointyhat-createJob',
      handler:      'createJob.lambda_handler',
      srcDir:       '../lambda/createJob',
      timeout:      cdk.Duration.seconds(29),
      memorySize:   128,
      // STATE_MACHINE_ARN injected after SF is created below
      environment:  { TABLE_NAME: jobsTable.tableName },
    });
    jobsTable.grantWriteData(createJob); // writes imageBase64 for large images

    // ============================================
    // resumeJob Lambda
    // Handles POST /resume.
    // Called when user picks a clarification chip.
    // Reads taskToken from DynamoDB, calls SendTaskSuccess on SF,
    // execution resumes at HackSearch with refinedQuery.
    // ============================================
    const resumeJob = makeLambda(this, 'ResumeJob', {
      functionName: 'pointyhat-resumeJob',
      handler:      'resumeJob.lambda_handler',
      srcDir:       '../lambda/resumeJob',
      timeout:      cdk.Duration.seconds(10),
      memorySize:   128,
      environment:  { TABLE_NAME: jobsTable.tableName },
    });
    jobsTable.grantReadData(resumeJob);  // reads taskToken
    jobsTable.grantWriteData(resumeJob); // clears taskToken after SendTaskSuccess

    // resumeJob needs sfn:SendTaskSuccess + sfn:SendTaskFailure
    resumeJob.addToRolePolicy(new iam.PolicyStatement({
      actions:   ['states:SendTaskSuccess', 'states:SendTaskFailure'],
      resources: ['*'],
    }));

    // ============================================
    // checkJob Lambda
    // Handles GET /job/{jobId}.
    // Pure DynamoDB read — completely decoupled from SF.
    // Returns status + results/clarification/image_analysis.
    // Unchanged from previous architecture.
    // ============================================
    const checkJob = makeLambda(this, 'CheckJob', {
      functionName: 'pointyhat-checkJob',
      handler:      'checkJob.lambda_handler',
      srcDir:       '../lambda/checkJob',
      timeout:      cdk.Duration.seconds(10),
      memorySize:   128,
      environment:  { TABLE_NAME: jobsTable.tableName },
    });
    jobsTable.grantReadData(checkJob);

    // ============================================
    // Step Functions — state machine tasks
    // Each LambdaInvoke passes full event payload through.
    // outputPath: '$.Payload' unwraps the Lambda response
    // so downstream states see clean output not wrapped in
    // { statusCode, body } envelope.
    // ============================================

    // Fail state — terminal, reached via addCatch on any task
    const jobFailed = new sfn.Fail(this, 'JobFailed', {
      causePath: sfn.JsonPath.stringAt('$.error'),
      errorPath: sfn.JsonPath.stringAt('$.errorType'),
    });

    // writerLambda task factory — reused at each write point
    const makeWriterTask = (id: string, writeType: string) =>
      new tasks.LambdaInvoke(this, id, {
        lambdaFunction: writerLambda,
        payload: sfn.TaskInput.fromObject({
          writeType,
          jobId:     sfn.JsonPath.stringAt('$.jobId'),
          data:      sfn.JsonPath.objectAt('$'),
        }),
        outputPath: '$.Payload',
      }).addCatch(jobFailed, { resultPath: '$' });

    // PENDING — first state, writes initial job record
    const writePending = makeWriterTask('WritePending', 'PENDING');

    // Vision task — image path only
    const visionTask = new tasks.LambdaInvoke(this, 'VisionTask', {
      lambdaFunction: visionLambda,
      outputPath:     '$.Payload',
    }).addCatch(jobFailed, { resultPath: '$' });

    // Ambiguity task — text path only
    const ambiguityTask = new tasks.LambdaInvoke(this, 'AmbiguityTask', {
      lambdaFunction: ambiguityLambda,
      outputPath:     '$.Payload',
    }).addCatch(jobFailed, { resultPath: '$' });

    // CLARIFICATION wait — used by both image and text paths
    // waitForTaskToken: execution pauses here until resumeJob
    // calls SendTaskSuccess with the token + refinedQuery.
    // heartbeatTimeout: if user doesn't answer in 30min → error
    // caught by addCatch → jobFailed → writerLambda FAILED
    const writeClarification = new tasks.LambdaInvoke(this, 'WriteClarification', {
      lambdaFunction:     writerLambda,
      integrationPattern: sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
      payload: sfn.TaskInput.fromObject({
        writeType:  'CLARIFICATION',
        jobId:      sfn.JsonPath.stringAt('$.jobId'),
        taskToken:  sfn.JsonPath.taskToken, // SF injects unique token
        data:       sfn.JsonPath.objectAt('$'),
      }),
      heartbeatTimeout: sfn.Timeout.duration(cdk.Duration.minutes(30)),
    }).addCatch(jobFailed, { resultPath: '$' });

    // HackSearch task — convergence point for both paths
    // resultPath stores output under $.hacksearchResult, preserving
    // jobId, refinedQuery etc from input for downstream Lambdas
    const hackSearchTask = new tasks.LambdaInvoke(this, 'HackSearchTask', {
      lambdaFunction: hackSearchLambda,
      payload: sfn.TaskInput.fromObject({
        query:         sfn.JsonPath.stringAt('$.refinedQuery'),
        jobId:         sfn.JsonPath.stringAt('$.jobId'),
        jobType:       sfn.JsonPath.stringAt('$.jobType'),
        refinedQuery:  sfn.JsonPath.stringAt('$.refinedQuery'),
      }),
      resultPath: '$.hacksearchResult',
    }).addCatch(jobFailed, { resultPath: '$' });

    // Ranker task — receives full state including hacksearchResult
    // passes jobId, refinedQuery, and hacksearch candidates to ranker
    const rankerTask = new tasks.LambdaInvoke(this, 'RankerTask', {
      lambdaFunction: rankerLambda,
      payload: sfn.TaskInput.fromObject({
        jobId:          sfn.JsonPath.stringAt('$.jobId'),
        jobType:        sfn.JsonPath.stringAt('$.jobType'),
        refinedQuery:   sfn.JsonPath.stringAt('$.refinedQuery'),
        hacksearchData: sfn.JsonPath.objectAt('$.hacksearchResult.Payload'),
      }),
      resultPath: '$.rankerResult',
    }).addCatch(jobFailed, { resultPath: '$' });

    // COMPLETE write — reads results + reasoning from ranker output
    const writeComplete = new tasks.LambdaInvoke(this, 'WriteComplete', {
      lambdaFunction: writerLambda,
      payload: sfn.TaskInput.fromObject({
        writeType: 'COMPLETE',
        jobId:     sfn.JsonPath.stringAt('$.jobId'),
        data: sfn.TaskInput.fromObject({
          results:   sfn.JsonPath.objectAt('$.rankerResult.Payload.results'),
          reasoning: sfn.JsonPath.objectAt('$.rankerResult.Payload.reasoning'),
          jobId:     sfn.JsonPath.stringAt('$.jobId'),
          jobType:   sfn.JsonPath.stringAt('$.jobType'),
        }).value,
      }),
      outputPath: '$.Payload',
    }).addCatch(jobFailed, { resultPath: '$' });

    // ============================================
    // State machine wiring
    // ============================================

    // Text path: ambiguity → clarification or skip to search
    const needsClarification = new sfn.Choice(this, 'NeedsClarification')
      .when(
        sfn.Condition.booleanEquals('$.is_ambiguous', true),
        writeClarification
      )
      .otherwise(hackSearchTask);

    // After clarification resolved → search
    writeClarification.next(hackSearchTask);

    // Shared tail: search → rank → complete
    hackSearchTask.next(rankerTask);
    rankerTask.next(writeComplete);

    // Job type router
    const jobRouter = new sfn.Choice(this, 'JobRouter')
      .when(
        sfn.Condition.stringEquals('$.jobType', 'image'),
        visionTask.next(writeClarification) // image always goes to clarification
      )
      .otherwise(
        ambiguityTask.next(needsClarification) // text goes through ambiguity check
      );

    // Full definition: PENDING → router → ... → COMPLETE
    const definition = writePending.next(jobRouter);

    // ============================================
    // Step Functions state machine
    // EXPRESS: max 5min, lower overhead, billed per duration.
    // Timeout covers worst case: vision(7s) + clarification wait
    // is handled by heartbeatTimeout not overall timeout.
    // ============================================
    const stateMachine = new sfn.StateMachine(this, 'PointyHatPipeline', {
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      stateMachineType: sfn.StateMachineType.STANDARD,
      // no stateMachineName — CDK generates one, avoids CloudFormation
      // replacement conflicts when definition changes require recreation
    });

    // createJob needs to start executions
    stateMachine.grantStartExecution(createJob);

    // inject STATE_MACHINE_ARN into createJob env after SF is created
    createJob.addEnvironment('STATE_MACHINE_ARN', stateMachine.stateMachineArn);

    // ============================================
    // API Gateway
    // POST /search        → createJob   (29s timeout for image payloads)
    // POST /resume        → resumeJob
    // GET  /job/{jobId}   → checkJob
    // ============================================
    const api = new apigateway.RestApi(this, 'PointyHatApi', {
      restApiName: 'PointyHat API',
      description: 'Hat search async API — Step Functions backed',
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: ['Content-Type'],
      },
    });

    const searchResource = api.root.addResource('search');
    searchResource.addMethod('POST', new apigateway.LambdaIntegration(createJob, {
      timeout: cdk.Duration.seconds(29),
    }));

    const resumeResource = api.root.addResource('resume');
    resumeResource.addMethod('POST', new apigateway.LambdaIntegration(resumeJob));

    const jobResource   = api.root.addResource('job');
    const jobIdResource = jobResource.addResource('{jobId}');
    jobIdResource.addMethod('GET', new apigateway.LambdaIntegration(checkJob));

    // ============================================
    // S3 + CloudFront — Frontend hosting
    // __API_BASE__ replaced at deploy time with API Gateway URL.
    // ============================================
    const websiteBucket = new s3.Bucket(this, 'WebsiteBucket', {
      bucketName:    `pointyhat-site-${this.account}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const distribution = new cloudfront.Distribution(this, 'WebsiteDistribution', {
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(websiteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      },
      defaultRootObject: 'index.html',
    });

    const htmlTemplate = fs.readFileSync('site/index.html', 'utf8');
    const injectedHtml = htmlTemplate.replace('__API_BASE__', api.url.slice(0, -1));

    new s3deploy.BucketDeployment(this, 'DeployWebsite', {
      sources: [s3deploy.Source.data('index.html', injectedHtml)],
      destinationBucket: websiteBucket,
      distribution,
      distributionPaths: ['/*'],
    });

    // ============================================
    // Outputs
    // ============================================
    new cdk.CfnOutput(this, 'ApiUrl', {
      value:       api.url,
      description: 'API Gateway base URL',
    });

    new cdk.CfnOutput(this, 'WebsiteUrl', {
      value:       `https://${distribution.distributionDomainName}`,
      description: 'PointyHat website URL',
    });

    new cdk.CfnOutput(this, 'StateMachineArn', {
      value:       stateMachine.stateMachineArn,
      description: 'Step Functions state machine ARN',
    });
  }
}
