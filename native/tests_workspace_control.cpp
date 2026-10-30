#include "workspace_control.hpp"
#include <iostream>
int main(int argc,char **argv) {
  if(argc!=2&&argc!=3)return 2;
  std::atomic<bool> stop{false};fw::WebSocket video(stop);
  if(video.readTimeoutMs!=15000)return 3; // generic/video constructor remains unchanged
  const double readinessDelay=argc==3?std::stod(argv[2]):0;
  auto endpoint=fw::parseURL(argv[1]);fw::WorkspaceControlClient client;
  const std::string bindings="[{\"src\":\"spatial:42\",\"stamp\":\"stamp\",\"generation\":\"panel-1\",\"inputEpoch\":\"input-1\"}]";
  client.start(endpoint);
  const double start=fw::nowMs();bool requested=false,admitted=false,bound=false;std::string notice;
  while(fw::nowMs()-start<readinessDelay+4500&&!client.failed) {
    client.hostFrame(fw::nowMs());
    if(!bound && fw::nowMs()-start>=readinessDelay){client.bind(bindings,true);bound=true;}
    if(client.active&&!requested){client.requestWindows();requested=true;}
    const auto catalog=client.catalog();
    if(!catalog.first.empty()&&!admitted){client.admit(catalog.first[0].selection);admitted=true;}
    if(catalog.second!=notice){notice=catalog.second;std::cout<<"notice "<<notice<<std::endl;}
    for(const auto &p:client.take())std::cout<<p.sequence<<" "<<p.kind<<" "<<p.x<<" "<<p.y<<" "<<p.dy<<std::endl;
    std::this_thread::sleep_for(std::chrono::milliseconds(25));
  }
  std::cout<<"ended "<<client.active<<" "<<client.failed<<std::endl;
  return client.failed&&!client.active?0:1;
}
