import { fresh, payload } from '../src/core.ts';
const b=fresh('Read a file','Explicit prompt','test','test','da1038c15ad5f010416924270ff4a0e17a15169f');
b.steps=[{kind:'model_call',name:'test/test',input:{messages:[]},output:{content:[{type:'text',text:'observed'}],usage:{input:10,output:2,totalTokens:12,cost:{total:0.1}}},duration_ms:10},
{kind:'tool_call',name:'read',input:{path:'README.md'},output:{content:[{type:'text',text:'evidence'}]},duration_ms:2}];
b.final='observed';console.log(JSON.stringify(payload(b)));
